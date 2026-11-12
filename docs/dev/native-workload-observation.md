# Read-only native workload observation

## Entry point

Use this command when desired VM state says stopped but installer, Showroom,
controller-created Pods, or paused VMIs may still exist. It writes a private JSON
report, performs Kubernetes GET requests only, and never changes quota accounting,
assets, ownership, workloads or enrollment. It is not a stop command.

## Prerequisites

- Use the repository backend environment with its Kubernetes/Pydantic dependencies.
- Supply an explicit operator-owned static kubeconfig. Executable/refreshing auth
  plugins, symlinks and group/world-writable kubeconfig files are rejected. Do not
  change shared credentials or RBAC just to run this diagnostic.
- The credential needs GET of the `kube-system` and project Namespaces, and LIST
  of TroshkaProject, VM, VMI, Pod, Deployment, ReplicaSet, StatefulSet, DaemonSet,
  Job, CronJob, PVC and DataVolume in the project namespace. Secret access, exec
  and storage-backend access are not needed.
- Start with a previously verified project root identity. Full project UUID and
  CR metadata generation are required; generation is a Kubernetes observation pin,
  **not** a quota dispatch generation or settled fence epoch. Never infer ownership
  from a namespace prefix or truncated UUID label.

## Procedure

1. Prepare a private JSON file with this shape. Values below are synthetic; replace
   every identity from the verified enrollment and current metadata. The cluster
   key is the `kube-system` Namespace UID, not an API URL or provider alias.

   ```json
   {
     "collector_id": "operator-diagnostic-20261112",
     "root": {
       "project_id": "00000000-0000-4000-8000-000000000001",
       "cluster_key": "verified-kube-system-uid",
       "namespace": "verified-project-namespace",
       "namespace_uid": "verified-namespace-uid",
       "project_cr_uid": "verified-troshkaproject-uid",
       "deployment_generation": 2
     }
   }
   ```

   To inspect metadata without exporting the CR spec, use explicit flags:

   ```bash
   oc get namespace kube-system --kubeconfig="$HOME/path/to/static.kubeconfig" -o jsonpath='{.metadata.uid}{"\n"}'
   oc get namespace <NAMESPACE> --kubeconfig="$HOME/path/to/static.kubeconfig" -o jsonpath='{.metadata.uid}{"\n"}'
   oc get troshkaprojects -n <NAMESPACE> --kubeconfig="$HOME/path/to/static.kubeconfig" -o custom-columns='NAME:.metadata.name,UID:.metadata.uid,GENERATION:.metadata.generation,PROJECT:.spec.projectId'
   ```

   Compare these values against the verified root. A mismatch requires identity
   investigation, not automatic repinning. Exactly one matching CR is required.

2. Run the collector from the checkout, choosing a new report path:

   ```bash
   /Users/prutledg/troshka/src/backend/venv/bin/python3 \
     /Users/prutledg/troshka/scripts/observe_kubevirt_workloads.py \
     --root /private/tmp/native-workload-root.json \
     --kubeconfig "$HOME/path/to/static.kubeconfig" \
     --output /private/tmp/native-workload-report.json \
     --max-objects 1000 --timeout-seconds 60
   ```

   Exit 0 means all 11 named kinds were read completely and root checks passed
   before and after collection. Exit 2 means a private report was written with
   incomplete evidence. Exit 1 means input/output failure; parser errors also use
   exit 2 but print usage and do not write a report. Output is replaced atomically
   with mode 0600 and cannot replace the root input or kubeconfig.

3. Inspect the report with local tooling. Each `collections` entry carries its own
   timestamps, list resourceVersion and status. Resources expose name, UID, RV,
   owner UIDs, deletion state, phase, paused state and selected desired state.
   Raw specs, env, annotations, volume handles and exception bodies are omitted.
   Kube APIs return full objects in memory; the report persists only these fields.

   `potentially_active` includes paused and terminating Pod/VMI objects, even with
   terminal phase while deleting. `terminal_observed` is a direct reported phase,
   not independent physical cessation proof. `not_runtime` marks VM desired-state,
   controller and storage objects; it does not claim they cannot create writers.
   Missing phases remain unknown. Unowned Pods are included without label filters.

## Troubleshooting and limits

- Missing credentials, missing/deleted root, permission denial or unavailable API
  stays incomplete. Namespace absence never proves backend storage absence.
- Unsupported API versions stay unavailable. There is no silent version fallback.
- Each response is capped at 4 MiB; each list is capped by `--max-objects` (maximum
  10,000). Continuation tokens, duplicate names/UIDs and malformed records invalidate
  the entire kind. No page-following loop or retry runs. Increase the object bound
  only after inspecting the cause; a byte-limit response stays incomplete.
- A monotonic scan budget prevents starting more reads after expiry; each request
  uses at most 15 seconds of remaining budget. Socket/server behavior can extend
  wall-clock duration; this is not a validated quota-enforcement freshness SLA.
- The report is neither atomic across kinds nor full API-resource discovery. It
  does not inspect arbitrary CRs, other namespaces, node processes, storage, or
  already accepted controller work. Owner UIDs are evidence, not verified chains.
- All reports unconditionally keep `settlement_verified`, `capacity_release_allowed`,
  `storage_absence_verified` and `admitted_plan_verified` false. No importer or live
  accounting integration is provided. Do not derive zero reserved capacity from
  an empty inventory. Native quota enablement remains unsupported.

## References

- [Native quota plan](../superpowers/plans/2026-11-11-kubevirt-cow-quotas.md), Task 6.
- [Scoped quarantine acceptance](native-quota-quarantine-acceptance-2026-11-12.md).
