# Native controller stop acceptance — 2026-11-12

## Result and scope

The explicit-target maintenance command passed scoped Deployment acceptance on
the user-authorized ocpvdev01 development cluster. Source revision: `f04c40e3`,
following controller support in `c0854501`. Native quota enforcement remains off.
This is not full project shutdown, writer isolation, settlement or COW accounting.

The command supports owned Deployments, ReplicaSets and restricted StatefulSets
with replicas zero, and Jobs with suspend true. Existing VM behavior is preserved.
Bare Pods (including Showroom), DaemonSets, CronJobs and Rook profiles remain
unsupported. Unknown storage ownership blocks the operation, including unsupported
storage elsewhere in the same project namespace. StatefulSet claim templates and
delete-on-scale policies are rejected.

## Disposable fixture

Only namespace `troshka-controller-stop-20261112` was created for this test:

- Namespace UID: `aead56a3-6332-4703-a725-f99ca69c5f7f`.
- Empty TroshkaProject `stop-fixture`, UID
  `f745a7f4-8dae-433b-b945-ccb619daebe6`, project UUID
  `f350c13f-0a82-4a35-95dc-4033b0afbe37`, generation 1. Its non-deploying
  `action: destroy` and empty topology caused no workload deployment by Troshka.
- Deployment `stop-demo`, UID `3bf943e0-560d-4b7c-9bee-56fa1c8f4e27`, owned by
  that root. One diskless UBI container requested 5m CPU/16 MiB, limited to
  50m CPU/64 MiB, with ServiceAccount token automount disabled.
- No existing project, image, pattern, ownership record, disk or quota ledger
  was modified. No shared credentials, RBAC or controller configuration changed.

The fixture was explicitly managed for this test, with no independent Troshka
desired-state writer targeting its Deployment. This does not prove effective
writer isolation for ordinary projects. Existing dev credentials disable TLS
verification; urllib3 warnings were retained. This is not authenticated production
security evidence.

## Observed checks

| Check | Observed result |
| --- | --- |
| Initial fixture | One ready replica |
| Initial dry-run | Blocked before any PATCH; no attempt event |
| Compatibility fix | Missing list item type metadata requires GET of the exact object with matching UID/RV and expected type |
| Corrected server dry-run | `dry_run_accepted`; persisted replicas remained 1 and RV remained `2669400518` |
| Explicit apply | `desired_stop_observed`; desired replicas 0 and generation 2 |
| Independent runtime check | Original Pod deletion completed; namespace had no Pods, PVCs, DataVolumes, VMs or VMIs |
| Cleanup | Dedicated namespace deletion completed |
| Cluster health | No Available/Degraded status changes; pre-existing machine-config degradation remained |
| Previous comparison resources | Admission and unenrolled comparison Deployments remained 2/2 ready |

Private journals and metadata are in
`/private/tmp/troshka-controller-stop-acceptance`. The original blocked journal was
preserved. The corrected dry-run and explicit apply used distinct new journals.
All report release, settlement, physical-stop and storage-absence flags remained
false, including after the independent single-fixture runtime check.

## Validation and remaining gates

Independent review approved the controller implementation and its compatibility
fix. The final focused backend run passed 189 tests, including actual Kubernetes
typed-list serialization and changed-UID/RV regressions. The unchanged quarantine
classifier suite passed 226 tests before the compatibility fix; required code
quality checks passed for the final source.

Full Task 5b/6 still requires effective writer authority, all workload coverage,
durable intent reconciliation and controller quiescence. Retained-snapshot COW
measurement, freshness, backend absence and native baseline integration also
remain open. The command is an operator maintenance tool, not an enforcement
adapter. Separate identity reads cannot defeat concurrent privileged changes to
storage retention; externally verified maintenance remains required for apply.

See [command usage and limits](native-vm-halt.md) and
[quarantine acceptance](native-quota-quarantine-acceptance-2026-11-12.md).
