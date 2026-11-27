# Operator-managed collector transport

Approved in chat on 2026-11-27. Replace the external host timer with local
operator reconciliation and a central publisher pod. This does not enroll new
storage domains, change custody rules, or mark project/OCP readiness successful.

## Local authority

Each existing Troshka operator reads an explicitly installed ConfigMap named
`troshka-collector-maintenance` in `troshka-operator`. Its JSON `config` contains
`cluster_key`, `deployment` (namespace, name, uid, container), and `backend`
(the existing protected toolbox backend). Absence disables maintenance.
Only the approved Deployment UID and exact Pod/ReplicaSet controller chain can
authorize a replacement. Require a single Ready, nonterminating candidate.

The operator issues TokenRequest credentials only for
`troshka/troshka-storage-collector`, requests seven days, checks actual expiry,
and renews with less than 48 hours left. Its projected Kubernetes identity is
automatically rotated by Kubernetes. It can patch/escalate only the named
toolbox Role; the reconciler does not change subjects or access production Argo. Validate the
existing Role and binding, retarget exact pod names, then verify cluster UID,
Ceph FSID and pool IDs using the collector token before publishing.

Publish a Secret named `troshka-collector-transport` in `troshka-operator` with
one `transport.json` key: version=1, cluster_key, deployment_uid, pod_name,
pod_uid, token, expires_at (epoch seconds), ceph_fsid, pools. Status in a separate
ConfigMap exposes only readiness, reason codes, observed pod UID and expiry.
No credential appears in status, logs, CR status or events. One local operator
replica serializes reconciliation; optimistic resource versions prevent stale
writes. Failed verification publishes no new transport. Repeat reconciliation
can safely recover a previously retargeted Role.

## Central publication

A dedicated central publisher pod uses the backend image and existing provider
identities to read only each named transport Secret, validating TLS against the
protected collector kubeconfig, never provider verify_ssl=false defaults.
It validates the envelope against protected cluster/storage pins and the
explicitly approved Deployment UID before accepting the token and pod identity.
Provider credentials remain in the application trust boundary and are never
sent to a collector or stored in published transport objects.

The publisher preserves existing profiles and publishes immutable Secret and
ConfigMap revisions through the named Argo Application. Before rollout it
defers for active work; afterwards it verifies all consumers and protected
file hashes plus live transport health. A persistent journal and exclusive
lock prevent ambiguous partial publication from being retried blindly.
Missing enrollment is reported separately and never synthesized.

## Release and acceptance

Test expiry, changed Deployment UID, impostor pods, ambiguous rollout, RBAC
drift, storage mismatch, stale envelopes, partial publication and restart.
Release the complete change through main, named tag, dev and production.
Install approved local configs from authenticated cluster observations, enable
central reconciliation, and observe a successful publication and subsequent
no-change cycle. Preserve unrelated jobs and configuration throughout.
