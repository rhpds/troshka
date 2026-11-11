# Quota allocation coverage

Enforcement remains disabled by default. This documents the deliberately bounded
initial adapter, not a claim of support for every Troshka environment. Baseline
readiness must cover historical charges, resource allocations and pending work.
`quota_accounting.require_ready` also rejects existing unsupported live mappings.
No administration role bypasses admission. Policy administration remains separate
from permission to operate a project; reservations always charge its owner.

## Supported execution

Basic VM environments on an existing single nested/troshkad **shared-billing** host
are supported. Recognized backing providers are AWS/EC2, GCP, Azure and OCP Virt.
The host needs explicit usable metering rates. Guest CPU, RAM, attached virtual
disk capacity and one environment slot are counted once. Backing host capacity is
platform capacity, not a second copy of guest resource consumption.

`quota_operations.prepare` reserves before placement or asynchronous dispatch.
Placement receives the operation ID and revalidates it before host selection.
No automatic backing-host provision or multi-host fallback occurs in enabled mode.
The deploy/start worker commits a single dispatch claim before entering the
ordinary deployment pipeline. It compares the accepted resource digest and image
capacity evidence, and passes the operation ID through RQ or thread fallback.
Autosave/import cannot edit topology while an operation is pending/reconciling.
Provider commands also validate exact VM capacities and disk paths/capacities.

Library/snapshot disks need one unambiguously selected `LibraryItemDisk` with
verified `virtual_size_bytes`. Admission expands requested capacity before
reservation and records this evidence. Sparse object/file size is never used as
virtual capacity. Multi-disk selection, pattern-backed disks and unresolved
images are unavailable; no allocation proceeds on an estimated zero.

| Entry point / execution boundary | Enabled behavior | Regression evidence |
| --- | --- | --- |
| `projects.deploy_project` → `placement.place_project` → `deploy_project_async` | Reserve before placement, claim once, validate exact VM/disk requests | `test_deploy_denied_before_placement`; `test_supported_deploy_through_actual_worker_and_provider_observation` |
| Pattern auto deploy / `_start_auto_deploy` | Same prepare/placement/worker path | `test_pattern_auto_and_bulk_are_admitted_before_placement` |
| Pattern bulk / `_bulk_deploy_projects` | Independent owner admission for each project; denied projects stay drafts | Same pattern test |
| Project start → `start_project_async` | Fresh spending/resource checks; subtract aggregate usage only from a whole-project target | Full supported lifecycle test |
| Interrupted pending deployment / `quota_operations.recover` | Recover original never-claimed operation; ambiguous claimed work requires reconciliation | `test_queue_failure_retains_identity_and_changed_policy_blocks_delivery`; raw-worker and duplicate assertions |
| Project/VM stop, force-off, pause | Available despite cap/readiness failure; paused compute remains held | Lifecycle test; existing VM-operation/power contracts |
| Confirmed project destroy/undeploy | Fence creation; require terminal jobs, full domain inventory and absence of both possible VM directories before final accrual/release | Lifecycle and retained-charge tests |
| Never-dispatched empty draft cleanup | Durable non-creation evidence; no provider/readiness dependency | `quota_cleanup.complete_empty`; draft deletion contracts |

## Explicitly unsupported growth

These transitions return `409` with `reason=unsupported_quota_operation` at their
route/service boundaries. Unsupported resource/provider configurations fail closed
with an accounting-unavailable explanation before provisioning. Limits may still
be edited and drafts may still be authored. Reduced live topology cannot safely
use the current reconfigure engine, which may restart/recreate resources; use a
confirmed undeploy followed by deployment of the reduced target in the same
project. Lifetime spending is preserved.

| Surface | Boundary / reason | Regression evidence |
| --- | --- | --- |
| Individual VM start, resume/unpause, restart | Project routes and `vm_power.unpause_vm_on_host`; use project stop/start | Parameterized direct-VM route tests |
| Portal start/reboot | Portal route before agent power call; stop remains available | Portal/runner test |
| Disk wipe with restart | Reject before destructive wipe; wipe without restart remains available | Parameterized unsupported-transition test; disk-wipe contracts |
| Reconfigure CPU/RAM/disk growth or reduction; added/replaced VMs | Route and background service/worker deny; live transition lacks verified per-resource accounting | Unsupported-transition and worker tests |
| VM/project redeploy, in-place OCP recert | Route/worker deny; use confirmed undeploy then deploy | Direct-VM, transition and worker tests |
| Container/pod start/restart/create/redeploy | Routes, worker, topology preflight and agent boundary deny; spending adapter incomplete | Transition/worker tests |
| Workload/installer/Showroom runner or ops pod | Topology `clusters`/OCP flags rejected; runner and native pod launch denied | Ops-cluster edit test; portal/runner test |
| BMC/deferred external power | Reject topology BMC/deferred flags and BMC networks; existing mappings block readiness | Inventory readiness test; resource-mapper BMC tests |
| Native KubeVirt/operator CR/PVC/VMI allocation | Reject before placement and in native deploy/pod services; existing mappings block readiness | Inventory readiness/raw-worker tests |
| Live migration / evacuation's workload move | Migration service denies; temporary copy allocation has no quota map | Service guard plus shared agent allocation guard |
| Snapshot/pattern capture / offline file pull temporary qcow2 overlay | Source-pattern API/common capture service reject before buffer provisioning; `/nbd/export`, `/nbd/pull-flatten`, `/patterns/capture-direct`, `/snapshots/capture`, `/vms/file-pull-snapshot` agent boundaries deny | NBD API/service regressions; `test_temporary_capture_storage_cannot_bypass_agent_boundary`; existing snapshot tests run disabled |
| Library/pattern/snapshot import into topology | Metadata only; later admitted deployment owns provisioning; pending topology edits locked | Image-capacity and topology-edit tests |
| Legacy `api/disks.py` / `api/vms.py` CRUD | Metadata only, no provider allocation or ledger release. Legacy Disk rows on live environments block supported readiness/admission pending identity reconciliation | Disk CRUD contracts; supported adapter rejects legacy rows |
| Host power/resize/storage extension; shared storage pool provision/extension; caches | Platform backing capacity. These do not increase provisioned guest requests or refund workload usage. Dedicated billing hosts are unsupported for quota deployments | Existing admin/placement contract classification; no guest allocation bypass |

Native operator/BMC actions outside the backend cannot be guarded by an API route.
They are excluded through configuration/readiness rejection; they are not claimed
as covered allocations. Arbitrary privileged host/provider shell access likewise
is not an allocation API provided by this adapter.

## Dispatch, observation and failure semantics

`QuotaReservation.dispatch_metadata` retains kind, resource digest, verified image
sizes, exact VM/disk map, claim, provider-call tickets/job IDs/terminal evidence,
worker completion timestamp, deployment generation ID, cancellation fence and
latest queue/thread delivery. A fresh deploy creates a generation; project starts
inherit it. Proven released generations retain history while no longer gating
current starts, known-empty admission or observation completion. Zero-delta start
operations remain part of a retained generation's observation fences.
It contains no credentials or provider stdout. A mutation intent commits before
HTTP dispatch. Lost responses retain an ambiguous ticket. Growth POSTs do not
automatically retry, and a repeated command/resource identity cannot dispatch
again under the original claim. Queue redelivery cannot acquire another claim.
Pending reservations survive queue failure and are discoverable for recovery.

Only an original trusted observation after **every** relevant worker's terminal
completion can change compute accounting. Complete all-on confirms reserved
capacity; complete all-off releases compute while retaining storage/slot. Mixed,
unknown or incomplete power retains the conservative allocation/reservation.
Partial individual shutdown is deliberately conservative; it does not use an
aggregate project quantity as a single-VM baseline. New operations serialize with
observation batches so old off evidence cannot release a newer zero-delta start's
capacity. No resource is released from project.state, timeout or metadata removal.

Deletion no longer freezes meters before asynchronous destruction. Cleanup can run
with unavailable accounting or exhausted limits. Only confirmed full absence closes
source meters, accrues through the original observation boundary, projects the
final invoice and releases allocations. Partial failures retain metadata, meters,
charges and ledger. Unknown/missing hosts and unsupported cleanup adapters retain
accounting for later reconciliation; they do not assert absence. Historical
charges and allocation identities remain independent of project deletion.
Cleanup locks the owner, every retained/current organization and the project
before source intervals. Closed source intervals can accrue and project a final
invoice after organization unassignment using their retained attribution. Missing
attribution is never replaced with an invented organization, and this cleanup
mode cannot admit an open live meter or allocation growth.
