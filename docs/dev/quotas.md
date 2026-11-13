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

The nested adapter does not cover native/operator KubeVirt, BMC/deferred power,
multi-host placement, dedicated billing, containers/pods, installers/Showroom/runners, live reconfiguration,
individual VM start/reboot/resume, live migration, snapshot/capture buffers,
unresolved image virtual sizes, legacy Disk rows and retained EIPs are not covered
by this baseline. Those growth surfaces are explicitly denied with enforcement
enabled. Existing unsupported live resources **block readiness**; denial of future
growth does not account for already allocated infrastructure.

## Fresh-era current-capacity import

After Unified Groups fresh accounting conversion, select
`--baseline-kind fresh-capacity` explicitly. The legacy importer remains available
for databases without the conversion receipt; it cannot reconstruct history on a
fresh database. Preflight reports the required kind and version automatically.
Use an explicit private target through `--database-url` or
`QUOTA_BASELINE_DATABASE_URL`; the CLI never defaults to the application's DB.

```bash
python3 scripts/backfill_quotas.py --preflight
python3 scripts/backfill_quotas.py --baseline-kind fresh-capacity --inventory
python3 scripts/backfill_quotas.py --baseline-kind fresh-capacity --evidence /private/operator/current-capacity.json
python3 scripts/backfill_quotas.py --baseline-kind fresh-capacity --evidence /private/operator/current-capacity.json --apply
python3 scripts/backfill_quotas.py --baseline-kind fresh-capacity --check-ready
```

Inventory gives the exact source digest, immutable fresh receipt identity,
retained operation IDs/generations and deterministic first-import tokens. Supply
the `fresh-capacity-v1` evidence envelope from the reviewed contract with stopped
Troshka writers, original job reconciliation and independent current compute and
physical storage observations. The manifest cannot override resources, owners,
organizations or rates. Dry-run rehearses the same atomic transaction and rolls it
back. Any blocked candidate rolls back the entire apply. Exact replay does not
rewrite rows or timestamps; newer evidence reconciles the original generation.
Pre-enable readiness still requires the reviewed source/ledger fingerprint.
After authorized enablement, ordinary billing cursors, charges and reservations
may evolve: readiness checks current supported attribution, ledger pairing and
fresh retained physical evidence. Exact replay remains bound to its original
post-import fingerprint even after those legitimate runtime changes.

The importer preserves projects, owners, organizations, images, patterns,
snapshots, original requested vectors, job identities, rates and every billing
source/cursor/document. It imports no historical costs. It records observed
capacity even above policy and reports the overage without dispatching shutdown.
`capacity_ready` covers present physical/compute proof; `accounting_ready` covers
current source/rate coverage; `runtime_ready` covers implemented admission and
shutdown profiles. Public `ready` requires all three. Enforcement stays disabled.

Nested evidence uses the supported shared single-host VM resource/power profile
and allocated file inventory for its existing generation. Native evidence uses
current enrolled roots and the latest complete COW graph. Stopped native assets
also need a complete independent managed resource inventory validated against the
existing stopped intent and producer-drained snapshot, covering guest and helper
shutdown. Missing helper observations, unsupported snapshot allocation ancestry,
unknown ownership, unresolved work and inventory drift remain blockers. A first
native import needs that off proof under its proposed generation token; this
importer cannot create an intent, collect provider evidence or stop workloads.
Configured VM/container CPU and RAM belong to user quotas; network, console and
virtualization overhead belong to platform capacity. A capacity import alone does
not authorize native execution: the bounded retained resume profile below must
also have verified demand, current billing coverage and resolved worker history.

### Retained managed native resume (increment 4A)

An already retained native project can resume through its original shared native
host after a reviewed fresh-capacity baseline, a complete enrolled physical COW
measurement and independent producer-drained off proof. Supported workloads are
basic VMs and single containers with ordinary networks and their managed
dnsmasq/gateway/exec/VNC helpers. VM CPU/RAM and configured container requests
(defaults: 1 CPU and 512 MiB) count toward tenant quotas; fractional CPUs sum
exactly and round up once per environment. Helpers remain platform compute and
all managed helpers participate in stop, restore and observation. Missing helper
limits do not invent tenant charges or cause runtime limits to be added.

Start reserves only restored compute under project, owner and organization limits.
The measured retained storage and environment slot stay on the original ledger
generation; their start increment is zero. One worker claims the operation and
persists a handoff before publishing the exact stopped episode and restore target.
The operator restores saved replicas, original VM power and validated bare
container manifests. Intentionally halted VMs stay halted. A complete independent
running/readiness inventory confirms the increment and opens configured tenant
compute meters at that proof time. Existing vCPU/RAM rates and provisioned disk
GiB-hour prices remain unchanged; measured physical COW bytes remain the storage
quota basis. No historical costs are migrated or repriced.

Pending helpers, paused/unknown workloads, changed resources or retained identities
keep the reservation. An ambiguous claimed handoff is observed rather than
redispatched. The existing enforcement poll retries observations and completion
receipts. Completed managed roots keep ordinary producer mutations fenced; only
the bounded lifecycle reconciler acts, preventing new topology growth or accidental
starts of intentionally halted VMs. A newer managed stop supersedes restore and
retains disk/PVC/image/pattern ownership and storage charges.

Ordinary project Stop, Force stop and auto-stop timers use that same durable
full-inventory managed shutdown. A request remains pending or failed until a
fresh complete observation proves tenant and helper compute off and recreation
suppressed. CPU/RAM allocation is retained until that proof; storage and the
environment slot remain charged afterward. Native pause/hibernate are rejected
before mutation with quotas enabled. The stop coordinator survives worker loss
and does not use a deployment job reference as its completion proof.

Native Delete and Undeploy use a durable `native-cleanup-v1` intent on the
original quota operation. Supported originals must have the existing managed
namespace-create receipt (`native_deploy.base_grants_mode=managed` and exact
`create_namespace_uid`) and a current complete supported storage graph. External
namespace/grant mode and older allocations with unknown namespace ownership
reject before cancellation, shutdown or destruction; current configuration never
overrides original external/unknown ownership. Unsupported snapshot ancestry,
capture or ambiguous storage shapes remain unavailable.

For an originally managed namespace, cleanup recognizes only the existing exact
Troshka provider/operator base grants and the observed OpenShift defaults:
`system:deployers`, `system:image-builders`, `system:image-pullers`, and
`default`/`builder`/`deployer` ServiceAccounts. Role references and subjects must
match the same original namespace. Generated internal-registry dockercfg Secrets
require the exact bootstrap ServiceAccount UID owner reference, matching platform
annotations and the ServiceAccount's reverse secret reference; a name prefix is
insufficient. Seal and recheck these immutable identities, and preserve bootstrap
resources until namespace deletion. The exact unowned `kube-root-ca.crt` publisher
shape and `openshift-service-ca.crt` service-CA injection shape are also sealed
and preserved; cleanup never individually deletes these live publisher maps.
Foreign, changed or additional unowned
resources block destructive teardown. This is not compatibility with arbitrary
namespace customizations or externally owned namespaces.

The original Project, owner, organization, host, slot and storage charges remain
visible as `cleanup_pending`. Full managed Stop proof closes compute first.
Scoped operator/CSI teardown uses sealed original UIDs and resource versions,
preserves shared images/patterns/caches, and never forces finalizers or directly
deletes backend images. Original child cleanup markers are independently read
back before parent deletion so foreground garbage collection cannot invoke
legacy name-based cleanup. After interruption, fresh original-state observations
may complete missing marker writes or UID/RV-bound submissions. Accepted/deleting
originals are observed without duplicate mutation. Contradictory, unknown or
replacement observations remain pending; retries never adopt replacements.

The existing managed collector continues observing the original root after its
namespace disappears. Retained original backend keys remain in the graph. Only
bounded, stable public active-image name/immutable-ID inventories plus
`rbd trash ls --all` can establish backend absence; command errors and namespace
404 are never zero. Retain-policy PVs/storage remain pending and charged.
Partial/stale/latest failed evidence retains prior charges. Fresh complete
original resource absence and backend settlement close storage/slot accounting,
freeze billing and finalize once: Undeploy resets the same record to draft;
Delete removes it. The receipt survives Project deletion on the existing
FK-free reservation row. Settled roots remain in append-only history while
receipt-authorized active-root selection permits successor enrollment. Late old
queue deliveries cannot change a later generation. Organization transfers and
configuration edits reject while cleanup is pending.

An empty never-deployed draft still uses the existing confirmed-empty path.
Native disk wipe is also rejected even when automatic restart is false, because
the wipe helper may power on a stopped VM before touching its disk.

### Managed first native deployment (explicit startup allowance)

The code integrates admission, immutable payload/rendered-plan and helper-image
pins, one claimed pending CR handoff, full UID binding, automated roots-only
enrollment, halted storage initialization, independent complete physical collection,
runtime release and independent running confirmation. The supported shape is one
shared native host, default pod networking, basic VMs/single containers and new
blank PVCs or the explicit managed central-image clone profile below. Ordinary
owners use automatic placement within their project's selected native provider;
the chosen host is frozen before reservation and revalidated before dispatch. Custom
`networkNode`, pod groups, installers, BMC, UEFI/TPM, ordinary imports, Ceph appliances,
multi-host and topology growth remain unsupported. A pending operation cannot
switch profile, payload, owner/organization or target after reservation.

The pending create handoff establishes the two existing provider/operator namespace
RoleBindings before posting the project CR. Managed grant mode creates the
namespace and bindings; external mode verifies the existing namespace and exact
externally administered bindings without writes. The original namespace UID is
retained durably and checked again after grants and during CR binding. An ambiguous
handoff is observed rather than replayed; no cluster-wide write grant is added.

Usage readiness runs the same active baseline selection/checks as admission,
including fresh-capacity receipt, completeness and freshness. Disabled enforcement
still reports `disabled` regardless of baseline readiness. Frozen invoices and
final statements always show their stored amounts and lines in owner and group
views, independently of current capacity readiness. New-era documents already
freeze ledger totals when created; reading historical documents does not rebuild
pre-cutover charges from the fresh ledger.

`native_initialization.implementation()` supports the explicit
`odf-rbd-blank-startup-allowance-v1` and `odf-rbd-clone-startup-allowance-v1`
profiles with the supported ODF installation
observer, Ceph server/client 19.2.1, format-2 non-thick blank PVCs and one exact
protected target. It requires `reservation_semantics: measured-startup-allowance-v1`
and a positive, operator-configured `initial_storage_bound_bytes` **per new PVC**.
Despite its legacy field name, that amount is a startup allowance, not a physical
maximum. There is no numeric default and no capacity-sized tenant reservation.
PVC logical capacity remains an applicability/CSI input; a large thin disk can
reserve a small allowance and subsequently settle to its measured allocated bytes.

The clone profile additionally requires `version: native-clone-initialization-v1`,
`operation: managed-cache-clone`, and this protected `source_policy` envelope:

```yaml
source_policy:
  version: central-qcow2-direct-import-v1
  provider_id: <approved-active-s3_readonly-provider-UUID>
  bucket: <approved-central-bucket>
  key_prefix: <approved-library-key-prefix>
  cache_secret_ref: <existing-cache-namespace-S3-secret-name>
```

These fields accompany the existing complete installation target, named VNC
image, applicability bounds and explicit allowance; they do not replace those
pins. The enrolled domain must have one exact protected cache namespace. Its
namespace, source-import Secret, provider credentials and read-only collector
credentials remain administrator setup. No source/secret upload API is added.
Only ready central QCOW2 LibraryItems accessible to the current project owner are
supported. The API resolves their database keys, freezes S4 HEAD identity and
measured QCOW2 virtual size, and ignores topology-derived source hints as authority.
Unknown virtual size is denied. Source import sizing is finalized once; the real
clone builder's padding and source-capacity floor are frozen before reserving the
per-claim startup allowance (a 30 GiB request normally renders 40 GiB).

The existing claimed operation reserves before the platform cache import. The
worker posts that import once using only bounded Kubernetes/S4 checks and fresh
protected installation evidence. The scheduled collector independently verifies
the original direct non-populator import twice, publishes its protected receipt
through additive enrollment, and verifies the original operator-created clone DV
and its CSI head → private intermediate snapshot → golden snapshot chain twice.
Workers receive no backend credentials. Initial and delayed runtime admission both
require that exact protected lineage and a fresh complete measured report. A
Succeeded DV, a copied disk, or an unverified source is insufficient. Golden bytes
remain platform-owned; tenant head and private intermediate bytes remain charged
through retained stop/resume and cleanup settlement. Ambiguous creates never cause
source recreation or adoption of a same-name clone DV.

This is a source-supported profile awaiting installed-CDI acceptance. The installed
CDI must honor `cdi.kubevirt.io/storage.usePopulator: "false"` for the new import and
clone, and produce the explicitly supported generated-PVC and dynamic CSI clone
provenance. PV request identity may differ from the final PVC UID after a supported
rebind; original DV ownership, new dynamic provisioning, exact CSI journal mapping
and the verified backend chain are still required. Unknown populator/snapshot or
adoption shapes stay blocked. Unit/transport fixtures do not authorize deployment,
quota enablement or historical source adoption.

This policy explicitly accepts temporary storage overage during creation. Every
complete measured byte stays charged. Allocation above the allowance retains the
assets and keeps workloads off; below it, fresh complete writer-free evidence and
quota revalidation permit runtime. Pending concurrent creates each reserve their
allowance. These reservations do not bound aggregate physical overage. Legacy
profiles lacking the explicit policy or helper reference reject validation and
cannot silently authorize overage. Synthetic transport tests prove integration,
not live deployment acceptance or a physical upper bound.

The profile's required `vnc_image` is a named version tag in
`quay.io/redhat-gpte/troshka-vnc-proxy:v...`; `latest`, digest references and other
repositories reject. The operator must verify the chosen tag exists before
authorizing it. `validation_reference`, `validation_sha256` and
`bound_justification` record the reviewed installation evidence, explicit policy
decision and allowance rationale; they do not certify a maximum. Protected
enrollment pins remain the authority for the amount, helper and exact target;
changing them requires the existing explicit enrollment authorization digest.
A collector report, PVC Bound status or the observed zero-byte sample cannot
authorize a profile. No real enrollment, scheduler or enforcement is enabled by
this source change.

Native admission uses measured project, owner and organization usage plus pending
tenant reservations. It does not reserve pool-wide free space or promise a
separate same-metric pool headroom guarantee. Existing provider placement and CSI
provisioning/capacity failures still apply. A platform capacity failure retains the
operation and assets for reconciliation; it never becomes zero tenant usage.

The production observer independently reads the existing provider API and protected
Ceph reader. Its bounded supported shape is the ODF `openshift-storage` RBD driver,
`-ctrlplugin` Deployment and `-nodeplugin` DaemonSet, Immediate blank-PVC classes,
RBD format 2 and disabled thick provisioning in one configured pool. It reads the
actual StorageClass UID, parameters and options, cluster/provider identities,
driver/workload UIDs and specification hashes, ready owner chains and consistent
running immutable container image digests. `ceph versions` identifies running
servers separately from the `rbd --version` client; mixed or unavailable versions,
incomplete rollouts, missing permissions and stale/future sources reject.

Selected controller ReplicaSets must match the current Deployment template.
Running controller/node pod specifications must match that template after only
explicit static Kubernetes defaults, scheduler node assignment, the standard
service-account token projection/mount, default tolerations and the bounded
DaemonSet node-affinity injection. Arguments, environment, security contexts,
mounts and every other spec field remain exact. Only named controller hash labels
and network-status/SCC metadata annotations are excluded from pod metadata
comparison; an SCC annotation never excuses a changed security specification.
The observer also derives only exact host-network hostPort defaults, named
PriorityClass values, non-BestEffort memory-pressure tolerations proven from
configured CPU/memory quantities, and ServiceAccount imagePullSecret references
when the installed template omits them. Selected ServiceAccounts and named
PriorityClasses are freshly read without Secret data; their UID and used
configuration participate in the stable first/last observation and protected
workload configuration digest. Explicit template pull-secret references remain
exact and cannot be excused by the ServiceAccount.
Omitted preemption policy is derived from that same fresh named PriorityClass;
explicit template policy remains exact. Only an absent-template annotation
`security.openshift.io/validated-scc-subject-type=serviceaccount` is recognized as
SCC admission bookkeeping; configured annotations and all other values remain
subject to exact comparison.

When these dependencies are used, `spec_sha256` hashes the canonical envelope
`{spec, admission_dependencies}` rather than the workload spec alone. Dependencies
include exact SA namespace/name/UID/pull-secret references and PriorityClass
name/UID/value/preemption policy. Absent dependencies retain the prior spec-only
digest. Changing a used dependency therefore changes observed target identity;
existing dependent pins must be deliberately reviewed and repinned. No profile
or physical bound is activated by this compatibility support.
Unrecognized admission mutations, unproved priority changes, init/ephemeral
containers and DaemonSet templates with required node affinity reject as
unsupported. The observer does not claim these shapes were accepted on live ODF.

New profiles carry typed `csi_installation` and `storage_class_options` fields.
Their legacy `csi_version` slot must contain the explicit discriminator
`odf-rbd-installation-v1`: it identifies this observation contract, **not a CSI
vendor version**. ODF release labels and expected profile strings are never copied
into observed versions. Legacy serialized profiles/reports retain their digest
shape, but legacy-only profiles have no production observer or startup policy.
No real profile or numeric allowance is configured by this source. Installed
identity observation does not certify a physical initialization upper bound.
The blank-only first-deploy path separately verifies no tenant Pods/VMIs or
ReplicaSets, halted VMs, zero-replica helpers, exact original PVC inventory and
fresh complete physical evidence before runtime. Its release also rejects parent
images, snapshots, children, retained bindings and snapshot evidence. Unsupported
or incomplete graphs remain unavailable. This does not fence external cluster
administrators or backend writers; controlled installation/credentials remain
prerequisites for acceptance.

Startup storage allowances remain reserved until exact complete initialization evidence
consumes only that storage remainder. Original requests remain immutable. Every
measured byte stays charged, including above-allowance allocation, which retains
assets and blocks runtime. Provisioned GiB-hour disk billing opens once at verified
initialization; configured VM/container compute billing opens only after complete
running proof. Fixed VNC support uses the existing namespace permissions and joins
initialization, stop and resume identity proofs without tenant compute charges.
Stale/partial attempts block release and growth. Ambiguous creates/releases are
observed without redispatch or adoption/deletion of unrelated objects.

The privileged collector needs one-time scheduling/configuration, not a manual
administrator enrollment step for each project. After reviewed installed-target
and managed-lifecycle acceptance and explicit allowance authorization, run the
following existing CLI cycle from its existing protected credential environment
on a periodic scheduler within evidence freshness bounds (for example every 20
seconds). Configure the explicit reporting DB target, protected pins, backend
configuration and inventory once; the application receives no backend secrets:

```bash
python3 scripts/import_kubevirt_storage.py \
  --target-id "$NATIVE_REPORT_TARGET_ID" \
  reconcile-managed \
  --pins /protected/native-pins.json \
  --backend-config /protected/native-backend.json \
  --inventory /protected/native-inventory.json \
  --kubeconfig /protected/native-collector.kubeconfig \
  --output /protected/native-latest-attempt.json
```

Set `TROSHKA_NATIVE_REPORT_DATABASE_URL` in that protected collector environment
(or supply the existing explicit `--database-url` option). The cycle checks the
current domain's predecessor chain against the protected pins, independently
verifies admitted pending roots, copies the initialization envelope unchanged and
collects/imports the whole expanded domain. It never rewrites pins or adds an
initialization authorization digest. Concurrent predecessors use CAS; failed
attempts invalidate readiness while prior roots, allocations and costs persist.
No scheduler or real profile is enabled by this source change.

Snapshot-backed image/cache ancestry remains blocked
unless its physical allocation graph is complete; a successful clone or PVC virtual
size is not that evidence. Unknown physical allocation never becomes zero. This
development increment has backend/operator integration tests, not live cluster
acceptance or authorization to enable enforcement/deploy it.

## Nested filesystem storage contract and operating limits

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

Nested-host cold image downloads cannot run in an enabled quota deployment. A platform
administrator must populate and verify approved immutable base caches as a
separately authorized platform maintenance task, check pool headroom, and then
retry the deployment. Do not disable quota enforcement or bypass project growth
guards to warm a cache. A cache miss/corruption during deployment remains an error.
Native central-image imports use only the separately pinned managed clone path
described above, after installed-profile acceptance; this does not enable ordinary
downloads or arbitrary topology source URLs.

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
