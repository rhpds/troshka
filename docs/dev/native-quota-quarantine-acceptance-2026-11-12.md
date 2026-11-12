# Native quarantine acceptance — 2026-11-12

## Result and scope

Scoped functional acceptance passed on the user-authorized shared development
cluster `ocpvdev01` (OpenShift 4.20.8). This is admission-service acceptance,
not production credential isolation, physical COW accounting, or authorization
to enable native quotas. No existing project, image, pattern, ownership record,
or shared operator RBAC was changed.

The service image was built from operator source at `0591c8b1` in the cluster:
`image-registry.openshift-image-registry.svc:5000/troshka-quarantine-build-20261112/quarantine-operator:v2026.11.12-acceptance-1`.
Subsequent local changes affect the offline policy renderer, tests and docs.
The current rendered acceptance bundle is `bundle-r4` in the private local
evidence directory `/private/tmp/troshka-quarantine-acceptance`.

## Observed checks

| Check | Result |
| --- | --- |
| API-server policy type checking | No expression warnings |
| TLS and service reconciliation | Two admission replicas ready |
| Enrolled namespace growth | ConfigMap create denied |
| Unenrolled namespace | Create accepted; comparison Deployment remains 2/2 ready |
| Cleanup-only update | Deployment scale to zero accepted in server dry run |
| Mixed cleanup and unrelated mutation | Denied |
| Growth and interactive access | Scale-up, VM start and Pod exec denied |
| Namespace label changes | Did not bypass quarantine |
| Controller recreation | Deleted fixture Pod was not recreated; ReplicaSet reported admission denial |
| Service outage | Enrolled create/update failed closed with zero endpoints; unrelated create succeeded |
| Deletion during outage | Fixture Deployment deletion and dependent cleanup succeeded |
| Recovery | Admission service restored to 2/2 ready |
| Namespace lifetime | Fixture namespace deleted; server dry-run recreation denied by lifetime policy |
| Status compatibility | Four live dry-run cases passed: allowed revision/network annotations and denied unrelated annotations |

The VM fixture used a 64 MiB empty disk, no PVC, and remained stopped. Its start
request reached the KubeVirt subresource API and was rejected through downstream
admission; this does not establish coverage of every aggregated or BMC path.
The VM was deleted before namespace cleanup. Lifetime denial was checked after
service recovery; namespace recreation during service outage was not repeated.

## Compatibility changes

Root admission requests may omit `subResource`; root matching now accepts absent
or empty values, while subresource matching still requires the exact value.

The bounded Deployment-controller status path permits only the revision annotation
`deployment.kubernetes.io/revision`. The bounded Pod-status paths permit only
`k8s.ovn.org/pod-networks` and `k8s.v1.cni.cncf.io/network-status`.
Both directions of the annotation comparison preserve unrelated keys. Spec,
identity, labels, ownership, finalizers and other metadata restrictions remain.
Other controller status paths retain full annotation equality.

Audited node identity `system:node:ocp-virtdev1-host4` was added to the existing
bounded cleanup role for final deletion of terminated service Pods. This changed
only acceptance policy configuration, not RBAC or enrolled-project exceptions.
Independent review found no concrete regressions in the compatibility diff.
The guarded operator suite passed: **1,622 tests**. Kubernetes clients are blocked
by the suite's autouse fixture; local HTTPS test sockets were permitted. The
targeted bundle suite also passed all 84 tests. `git diff --check` was clean.

## Cluster state and retained resources

All cluster operators remained Available. The machine-config operator was already
Degraded before testing and remained so afterward: host2 belongs to two custom
roles. No new operator degradation was observed.

- `troshka-quarantine-fixture-20261112` is deleted. Its UID was
  `a3cd5d21-536f-44ef-ad35-29a15c89413f`.
- `troshka-quarantine-control-20261112` retains the two-replica admission service,
  TLS Secret and immutable enrollment configuration. Its UID is
  `5914c10c-45fe-45cc-9711-48782bf7c259`.
- `troshka-quarantine-build-20261112` retains the completed build and image.
- `troshka-quarantine-unenrolled-20261112` retains the small comparison workload.
- Active `troshka-quarantine` webhook and `troshka-quarantine-{lifetimes,control,webhook}`
  policies/bindings remain. Three unbound `*-preflight-20261112` type-check policies
  also remain; they have no bindings and enforce nothing.

Do not remove the lifetime policy or enrollment merely to reuse the deleted
namespace name. Follow the ordered rollback in the acceptance runbook for further
cleanup. The active webhook matches only the dedicated fixture namespace; control
protections match only dedicated control resources and the named webhook.

## Remaining enablement requirements

Existing shared credentials can read control Secrets; this explicitly authorized
dev test did not harden them and cannot prove credential isolation. Production
enablement still requires effective-authority isolation, full writer coverage,
controller quiescence, fresh COW measurements including retained snapshots, and
settlement/absence evidence before releasing reserved capacity. Parent native
Tasks 5b, 6, 7 and 8 remain open. Quota enforcement was not enabled by this test.
