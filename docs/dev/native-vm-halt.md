# Native VM and helper controller maintenance requests

This standalone command sends an exact desired-state halt request for explicitly
selected existing native VMs or supported helper controllers. It defaults to Kubernetes server dry-run. It is not
a project stop command, a controller fence, or proof that compute stopped.
It does not integrate with admission, billing, quota enforcement or reservations.

## Preconditions

- Use the repository backend Python environment and an explicit static kubeconfig,
  with the same file/auth restrictions as [workload observation](native-workload-observation.md).
- Independently establish and verify a writer maintenance boundary before apply.
  An effective, independently verified quarantine is one possible part of that
  boundary; this command does **not** install or verify it. Merely passing a flag,
  supplying a root manifest, or receiving dry-run success does not fence writers.
- Credential permissions: the observer's GET/LIST permissions, plus GET on
  TroshkaVMs, PVCs, DataVolumes and cluster-scoped PVs, and PATCH on the selected
  VirtualMachines and selected apps/v1 Deployments, ReplicaSets, StatefulSets or
  batch/v1 Jobs. Controller profiles additionally GET TroshkaNetwork CRs.
  Dry-run still requires PATCH authorization. No Secret reads,
  exec, storage-backend writes, delete permissions or credential changes are needed.
- Obtain full project/namespace/cluster identity as described in the observation
  guide. For each authorized target obtain exact VM name, UID and resourceVersion:

  ```bash
  oc get virtualmachines -n <NAMESPACE> --kubeconfig="$HOME/path/to/static.kubeconfig" \
    -o custom-columns='NAME:.metadata.name,UID:.metadata.uid,RV:.metadata.resourceVersion'
  ```

## Supported retained-storage profile

The command verifies the generated ownership chain: VirtualMachine -> TroshkaVM
-> pinned TroshkaProject. Every link has exactly one controller owner reference
with matching name, UID, kind and API version. Those retained objects must not be
deleting. Namespace prefixes and mutable labels do not prove ownership.

VM and matching VMI volume sources may be existing `persistentVolumeClaim`
references or `cloudInitNoCloud` (including the generated Secret reference; its
contents are never fetched). Each referenced bound PVC must be owned by the
verified TroshkaVM or project, or by a verified DataVolume owned by that TroshkaVM.
Its nondeleting PV must have the exact PVC claim UID/name/namespace and no owner
references. No disk, PVC, PV, DataVolume, image, pattern, owner or sharing setting
is changed by the command. A PV reclaim policy of Delete is not a reason to delete
its PVC; this command never issues deletion.

DataVolume templates, ephemeral VM volumes, hotplug requests, unknown/mixed
ownership, runtime-owned PVCs and unknown launcher storage profiles block the
whole initial preflight. Matching launcher Pods permit PVCs from the verified VM
claim set plus emptyDir, Secret, ConfigMap, projected and downwardAPI volumes;
generic ephemeral PVCs are rejected. Other VM profiles require separate design,
not relaxed checks. The command never deletes VMIs/Pods, removes finalizers or
uses force/grace-zero shutdown.

## Supported helper controllers

The same manifest and command accept an optional target `kind`: `Deployment`,
`ReplicaSet`, `StatefulSet` or `Job`. Omitting it preserves the `VirtualMachine`
profile. Select each controller by exact name, UID and resourceVersion, for example:

```json
{"kind": "Deployment", "name": "gateway-verified-namespace", "uid": "00000000-0000-4000-8000-000000000003", "resource_version": "12345"}
```

Deployments, ReplicaSets and StatefulSets receive only `spec.replicas=0`; Jobs
receive only `spec.suspend=true`. An absent replicas/suspend field uses JSON Patch
`add`; an existing field uses `replace`. UID/RV tests and the unchanged admission
cleanup classifier verify every proposed and returned resource. Existing VM
behavior and statuses are preserved. Mixed manifests preflight every target
before the first request; each target is revalidated before its own request.

Controllers must be owned directly by the pinned Project or through a verified
TroshkaVM/TroshkaNetwork. ReplicaSets may additionally have a verified Deployment
parent. These are exact API-version/name/UID/controller-reference chains, never
label-based selection. Actual generated DHCP Deployment -> TroshkaNetwork ->
Project and gateway/exec Deployment -> Project profiles are tested from their
source builders. Guestfish Job -> TroshkaVM -> Project can use retained existing
VM-owned claims. Rook/Ceph roots, CronJobs, DaemonSets and bare Pods are refused.
Generated Showroom runs as a bare Pod and remains potentially active; this patch
does not stop it.

The bounded graph includes Deployment-owned ReplicaSets and their owned Pods,
or directly owned Pods for other supported controllers. Every selected runtime
owner is checked; deleting runtimes block the request. All current and historical
ReplicaSet templates and runtime Pod volumes must use existing PVC references,
emptyDir, Secret, ConfigMap, projected or downwardAPI sources. Unknown sources,
hostPath and generic ephemeral claims are refused. Secret contents are never read.
StatefulSets with nonempty `volumeClaimTemplates` or `whenScaled=Delete` are
refused; only absent/Retain scale policy is supported. Controllers themselves
remain present, including any `whenDeleted` retention setting.

Every referenced PVC must be bound, nondeleting and retained by the verified
controller, generated CR or Project, optionally through one DataVolume. Its PV
claim identity and lack of owner references are checked. Additionally, complete
bounded namespace PVC and DataVolume lists must prove known owner ancestry to
the pinned Project; runtime-owned, ownerless, unknown or cyclic ancestry blocks
the request even when unmounted. Traversal allows at most six links per object,
caches ancestor GETs, and caps cached nodes at eight times `--max-objects` within
the command deadline. This conservatively blocks unrelated unsupported storage
in the same project namespace. No fallback to deletion is implemented.

Scaling and Job suspension can delete runtime Pods through Kubernetes controllers.
Retention checks address this indirect storage garbage-collection risk; they do
not prove physical stop or prevent a still-authorized operator/HPA from restoring
desired state. The independently verified writer maintenance prerequisite remains
mandatory. The command changes no storage, owner references, images, patterns or
sharing settings. Separate reads cannot guarantee preservation against concurrent
privileged changes or arbitrary external-controller side effects.

Separate Kubernetes reads cannot atomically lock all storage ownership against
other privileged writers. External admission, controllers or guest shutdown
behavior may have effects beyond this request. The preservation checks are an
observed supported profile under verified maintenance, **not** an unconditional
storage-preservation guarantee under concurrent mutation. Full Task 5b writer
mediation and Task 6 quiescence remain prerequisites to native quota enablement.

## Run

Prepare a private manifest (synthetic identities below must be replaced):

```json
{
  "collector_id": "maintenance-20261112",
  "root": {
    "project_id": "00000000-0000-4000-8000-000000000001",
    "cluster_key": "verified-kube-system-uid",
    "namespace": "verified-project-namespace",
    "namespace_uid": "verified-namespace-uid",
    "project_cr_uid": "verified-project-cr-uid",
    "deployment_generation": 2
  },
  "targets": [
    {
      "name": "verified-vm-name",
      "uid": "00000000-0000-4000-8000-000000000002",
      "resource_version": "12345"
    }
  ]
}
```

Run server dry-run with a **new** output filename:

```bash
/Users/prutledg/troshka/src/backend/venv/bin/python3 \
  /Users/prutledg/troshka/scripts/halt_kubevirt_vms.py \
  --manifest /private/tmp/native-halt-targets.json \
  --kubeconfig "$HOME/path/to/static.kubeconfig" \
  --output /private/tmp/native-halt-dryrun-001.jsonl
```

After independently confirming the maintenance prerequisite and reviewing dry-run
evidence, the deliberate write form adds `--apply` and uses a different new output
path. That form is documented for future authorized use; no live apply was part
of the implementation validation.

All targets (maximum 32) must pass initial bounded inventory and retention
preflight before the first request. Each target is revalidated, including a fresh
root proof, before dispatch. JSON Patch tests VM UID and resourceVersion before
replacing only existing `spec.runStrategy` with `Halted` or `spec.running` with
false. Mixed running/runStrategy modes are rejected. The unchanged quarantine
classifier checks the proposed delta and returned full resource; its local use
does not prove a webhook is deployed or effective.

The create-new JSONL journal is mode 0600 and fsynced before every request. Existing
outputs, symlinks to existing paths and input/config aliases are refused. Each
`attempt` precedes the external request; `result` and `summary` follow it. Do not
reuse output filenames. No raw specs, Secret contents, API error bodies or
credentials are recorded. A summary includes the existing bounded observer's
post-operation evidence, including paused VMIs and other live workloads.

## Interpret results and recover

| Status | Meaning |
|---|---|
| `dry_run_accepted` | API accepted the dry-run and returned the exact supported cleanup delta. No persisted halt is claimed. |
| `desired_halt_observed` | API accepted the apply request and a fresh GET observed the same VM UID with desired halt. Compute may still run. |
| `halt_request_accepted` | API returned the expected applied resource; follow-up desired-state evidence was unavailable or changed. Processing stops. |
| `desired_stop_observed` | API accepted the controller apply request and a fresh GET saw the same UID with replicas zero or suspend true. Pods can still run. |
| `stop_request_accepted` | API returned the expected controller resource; follow-up desired-state evidence was unavailable or changed. Processing stops. |
| `ambiguous` | Request outcome or response cannot be trusted, including conflict, rejection, transport loss or altered response. Processing stops; no automatic retry. |
| `blocked` summary | Initial or fresh preflight failed. Earlier per-target records, if any, still describe already attempted actions. |

Exit 0 means all selected requests reached their documented dry-run/desired-state
result; it never means shutdown is complete. Exit 2 means blocked/incomplete. Exit
1 means input, client, journal or other command failure. Argument errors also use
exit 2 with usage and no report.

An attempt without a durable result is unresolved, even if the request may have
succeeded. Do **not** automatically rerun, repin RVs, roll back by restarting, or
infer failure from lease/time expiry. Inspect fresh read-only identity and VM/VMI/
Pod evidence, reconcile the particular attempt under maintenance, and obtain a
new explicit target selection before any further deliberate operation. A journal
write failure stops the process; an unrecorded response is not retrospective
success. A new filename alone does not resolve an old attempt.

GET and PATCH responses are capped at 4 MiB; redirects and HTTP retries are off.
All lists use the observer's maximum-object bound and reject continuation. The
default whole-command scan budget is 60 seconds (`--timeout-seconds` 1..300), and
each request has at most 15 seconds remaining budget. Socket behavior can extend
wall-clock duration; this is not a measured enforcement SLA. API versions are
explicit, with no compatibility fallback. Fresh evidence may still be non-atomic.

Every summary unconditionally leaves `settlement_verified`,
`physical_stop_verified`, `capacity_release_allowed` and
`storage_absence_verified` false. Paused or terminating VMIs remain potentially
active. Bare installer/Showroom Pods, DaemonSets, unsupported Jobs and other writers
remain observed. Supported controllers only receive desired-state requests; their
runtime absence is unproven. No ledger release, storage absence or complete project
shutdown can be inferred from this command.
