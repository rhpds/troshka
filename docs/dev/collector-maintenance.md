# Operator-managed collector maintenance

Collector credentials and toolbox pod pins are shared by backend, lifecycle,
tunnel, collection and readiness workloads. Init containers copy these into
protected regular files: changing a Secret alone cannot refresh running users.

## Local operator

The existing Troshka operator performs local renewal when the explicit
`troshka-operator/troshka-collector-maintenance` ConfigMap exists. Its `config`
JSON contains `cluster_key`, `deployment` (namespace, name, uid, container),
and `backend` (the existing protected toolbox backend).

Obtain Deployment and cluster UIDs from an authenticated administrator connection,
and copy the backend from the enrolled application's protected configuration.
Do not infer authorization from pod labels or create new enrollment automatically.
The operator verifies the exact Pod → ReplicaSet → approved Deployment chain,
requires one Ready candidate and preserves the existing dedicated collector
identity and exact-pod exec permissions. A replaced Deployment UID requires review.

Renewal requests seven days and checks actual JWT expiry, rejecting a lifetime
under four days. Renewal starts with less than 48 hours remaining. The operator
uses its projected service-account identity, which Kubernetes rotates. No
administrator kubeconfig or production Argo credential is mounted there.

Only after verifying cluster UID, Ceph FSID and pool IDs with the collector
credential does it publish the named `troshka-collector-transport` Secret.
The `transport.json` envelope includes the token and verified identities. Never
print this Secret. The separate maintenance status ConfigMap contains safe
readiness/reason/expiry information; alert on failed or stale reconciliation.

## Central publisher pod

Install a ConfigMap named `troshka-collector-maintenance` in the application
namespace, with a `plan.json` key shaped as follows:

```json
{"applications":[{"mode":"operator","namespace":"troshka","application":"troshka",
"transports":{"storage-cluster.kubeconfig":{"toolbox":{
"namespace":"openshift-storage","deployment":"rook-ceph-tools",
"deployment_uid":"APPROVED_DEPLOYMENT_UID","container":"rook-ceph-tools"}}}}]}
```

Include every existing profile's exact transport filename and independently
approved Deployment UID. This plan has no credentials. Set the named Argo
Application's Helm value `collectorMaintenance.enabled=true` after installing
the operator release and configuration. Set `collectorMaintenance.application`
to that Application's name. The chart defaults maintenance to disabled.

The dedicated publisher uses existing database provider identities only to read
the named operator output Secret. It uses the protected collector TLS endpoint
and CA, never the provider driver's insecure TLS defaults. Provider credentials
are never included in collector configuration or private publication journals.

Every cycle revalidates operator output against protected enrollment and storage
pins, then verifies the new credential directly. Changed inputs are published as
new immutable Secret/ConfigMap revisions through the named Argo Application.
The publisher defers for active RQ jobs, preserves old revisions for rollback,
waits for every consuming Deployment, and checks protected file hashes plus live
fleet health. The publisher does not mount those changing inputs itself.

The chart creates a retained RWO state PVC and a single Recreate Deployment.
An exclusive filesystem lock and fsynced `pending.json` journal survive pod
replacement. Partial/ambiguous publication remains blocked for review, never
blindly retried. Status is available in
`troshka-collector-maintenance-status` under `status.json`; monitor `ready`,
`status`, and the `observed_at` heartbeat. Healthy pod scheduling alone is not
maintenance success. Missing profiles report incomplete fleet status separately.

## Recovery and verification

Inspect safe status and private journal evidence after a failure. Reconcile
actual Argo references, immutable objects, and consumer file hashes before
archiving/removing `pending.json`. Never remove it merely to retry. Retain the
PVC and old immutable revisions until recovery has been verified.

Acceptance requires a successful update followed by a healthy no-change cycle,
not just a running publisher. Verify project receipts and VM state separately;
transport recovery does not prove completed accounting or OpenShift installation.

`scripts/collector_maintenance.py` remains an explicit administrator-operated
repair tool with read-only default and `--apply` opt-in. Automatic operation
uses `scripts/collector_controller.py` in operator-only mode; no external host
timer is required.
