# Native quota quarantine: server-side validation slice

This continues the approved native COW quota plan after Tasks 4 and 5a. The user
authorized continued implementation through the point where deployment is needed.
This slice ends at isolated API-server acceptance, not production enablement.

## Outcome and limits

Deliver an inactive-by-default admission service and explicit offline deployment
bundle for quarantining existing, independently verified namespace lifetimes.
The service denies growth regardless of the caller's original credentials. It
does not issue positive quota grants, mutate the intent store, acknowledge a
settled fence, stop running processes, or measure storage. Full writer mediation,
controller quiescence, cross-namespace cache work and COW freshness remain open.

## Global constraints

- Do not deploy, push, enroll live namespaces, create credentials or enable quotas.
- Preserve existing projects, images, patterns, ownership, sharing and cost history.
- No namespace is enrolled by default; existing install manifests remain unchanged.
- Admission is side-effect-free: no Kubernetes client, database, CAS or storage calls.
- Namespace names select requests; full namespace and project-root UIDs establish enrollment.
- No caller identity, mutable label or request annotation can authorize growth.
- DELETE requests remain outside the external webhook; deletion is not proof of release.
- Unknown or malformed enrolled requests fail closed with bounded nonsecret reasons.
- Use python3, preserve required hooks, and never add Co-Authored-By trailers.

## Authority and alternatives

Application wrappers cannot fence old provider/BMC credentials or independent
controllers. A mutation broker would require comprehensive credential revocation
and controller integration. Stateful admission also needs authenticated API-server
requests and final persisted receipts; AdmissionReview userInfo alone is not proof
of origin. Start with stateless quarantine instead. Synthetic HTTP calls can only
obtain decisions, never change authority or consume accepted-intent capacity.

The service runs as a separate Deployment using the existing operator image at
an explicitly named release tag. It does not import the operator process, mount
a service-account token or receive Kubernetes RBAC. Configuration is immutable,
read-only and outside enrolled project namespaces. Reusing the image avoids an
unnecessary new release pipeline; process and credential isolation are separate.

## Enrollment

Schema v1 contains mode `quarantine`, cluster_key (kube-system Namespace UID),
enrollment_id UUID, namespace_name, namespace_uid, full project_id UUID,
project_cr_name and project_cr_uid. Require all fields and reject duplicates,
conflicts, invalid identities, extra fields and unsupported modes. Limit a bundle
to 32 enrolled lifetimes. Input is operator-owned evidence, not an API payload.
The offline renderer cannot verify live ownership: the runbook must require
independent current UID verification and a writer-maintenance boundary before apply.
The renderer also requires `control_namespace_uid` and `scheduler_nodes`: a full
verified existing control Namespace UUID and a bounded nonempty list of exact
eligible node names. Control Namespace CREATE/DELETE always deny; UPDATE requires
the pinned UID and a bootstrap username. Pod binding targets and node affinity
use the explicit node names, never inferred usernames or mutable node labels.

The webhook uses exact request.namespace matchConditions and Namespaced scope.
It must not use namespaceSelector or objectSelector as an opt-in boundary.
An API-local ValidatingAdmissionPolicy permanently rejects Namespace CREATE for
each enrolled name and checks the original UID on existing Namespace mutations.
The tombstone survives deletion; name reuse and automatic reenrollment are denied.
This prevents an old provider from recreating a name and escaping its lifetime.

Because existing operators can restore broad RBAC, API-local protection must also
cover the control namespace and its resources. Only explicit bootstrap principals
and narrowly necessary controller operations may alter it. Wildcard administrator
groups, untrusted impersonation, service-account token theft and ordinary worker
RBAC must not become a documented bypass. Admission-policy/webhook configuration
is itself administrator-controlled. Effective isolation requires live acceptance.

Repository inspection found existing cluster-wide Secret reads and Rook
nodes/proxy permissions. Admission policies cannot deny GET. Production enrollment
requires a separate effective-permission audit and reviewed hardening before TLS
installation; this slice must not silently change those existing roles. The audit
includes access to bootstrap/controller namespace credentials and ability to
restore bindings. Synthetic-writer acceptance is not production isolation proof.
Kubernetes VAP cannot match ValidatingAdmissionPolicy or its Binding resources
([API restriction](https://kubernetes.io/docs/reference/kubernetes-api/admissionregistration/validating-admission-policy-v1/)).
Their protection therefore requires independently verified effective RBAC and
credential isolation; this slice does not introduce another admission technology.

## Request decisions

Strictly parse admission.k8s.io/v1 reviews, echo request UID, and validate operation,
resource, subresource, namespace, name and old/new object identities. Return only
reason codes; do not log request bodies or exception payloads. Enforce bounded
body size, nesting/work and request time. Reject duplicate JSON keys.

CREATE and CONNECT are denied. UPDATE defaults to deny. Supported cleanup is a
small exact whitelist: removal-only finalizers; apps/v1 Deployment/StatefulSet/
ReplicaSet replicas to zero (including their scale subresources); batch/v1 Job
suspend true; kubevirt.io/v1 VirtualMachine Halted or running false. Status-only
updates may be allowed only for explicitly enumerated built-in GVRs, not arbitrary
custom controllers. Compare full objects after removing only the explicitly
allowed changed fields and API-owned resourceVersion/managedFields metadata.
For the enumerated root spec cleanup only, require positive signed-64-bit integer
generation values: new generation equals old plus one when the allowed spec field
changes, or equals old for an already-stopped no-op. Missing, boolean, noninteger,
zero/negative, overflowing or other generation transitions deny. Remove generation
from the full-object comparison only after this validation. Status, finalizer and
scale-subresource comparisons retain generation equality without this exception.
All other spec, labels, annotations, owner references, identity and unknown fields
must remain equal. Reject cleanup combined with image, disk, template, metadata
or unrelated status changes. If a transition cannot be safely expressed, deny it
and document the limitation instead of broadening the whitelist.

DELETE is excluded at webhook routing and has a defensive allow response if called
directly. It remains subject to Kubernetes authorization. No auto-delete operation
is added. During service outage, UPDATE cleanup is unavailable; storage-preserving
emergency shutdown needs independently verified VMI/Pod deletion. Do not claim
project/namespace deletion preserves storage or that DELETE refunds quotas.

## Server and bundle

Use the existing pinned aiohttp dependency for a dedicated HTTPS entrypoint,
readiness and liveness endpoints. Require explicit config and TLS files; no
plaintext production fallback. No API token or write endpoints. Resource/security
limits and bounded parsing prevent untrusted reviews from unbounded work.

An offline CLI renders immutable config, separate Deployment/Service, dedicated
ServiceAccount with token automount disabled, disruption settings, a validating
webhook and API-local policy/binding objects. Require explicit existing control
namespace, named image release, TLS Secret reference, CA PEM and bootstrap
principals plus explicit verified controller identities. Controller exceptions
are restricted by resource, subresource and operation within the control namespace:
Deployment/ReplicaSet reconciliation, scheduler Pod binding, kubelet Pod status,
endpoint publication and disruption status. They never grant desired Deployment,
Service, config, TLS Secret, ServiceAccount or RBAC mutation. No guessed controller
usernames or blanket identity bypass. Never emit private keys or credentials, invoke kubectl, contact a
cluster or modify existing manifests. Reject latest/main/production and digest-only
image references. Group staged output into prerequisites/control protections and
activation resources; no automatic apply. Refuse empty/wildcard enrollment.

Controller status exceptions compare full spec and every persisted Kubernetes
v1.30 ObjectMeta field except resourceVersion and managedFields. Require presence
parity and value equality for name, generateName, namespace, selfLink, uid,
generation, creationTimestamp, deletionTimestamp, deletionGracePeriodSeconds,
labels, annotations, ownerReferences and finalizers. This field list targets the
built-in API schema; unknown metadata is handled by that schema, not compared as
arbitrary map keys by this policy. Target API type checking and real-controller
status/rollout acceptance remain mandatory, including managedFields creation and
timestamp/ownership updates paired with metadata/spec hitchhiking denials.

Fixed resource names support one isolated first installation per cluster. Existing
installation or tombstone history blocks the first-install runbook until cumulative
history is independently reconciled. Offline rendering cannot preserve unseen
enrollments and is not an automatic upgrade/uninstall path.

Set failurePolicy Fail, sideEffects None, timeoutSeconds 3, admissionReviewVersions
v1, matchPolicy Equivalent and exact namespace matchConditions. Intercept CREATE,
UPDATE and CONNECT across all namespaced groups/versions/resources/subresources.
Unrelated namespaces must be filtered by the API server before service calls.
Require Kubernetes v1.30+ APIs/capabilities; never weaken protection as fallback.

## Acceptance and deployment boundary

Local tests prove classification, bounded HTTP handling and rendered structure.
They do not establish CEL type checking, API-server routing, TLS, aggregated
KubeVirt subresources, effective RBAC or controller drain. Do not claim a static
YAML parse is a CEL/API validation. Prepare a concrete isolated acceptance runbook
with explicit kubeconfig and synthetic test resources, separating preview from
deliberate writes. Stop before its first deployment action.

Acceptance must test un-enrolled availability, growth denial with old writer/BMC
credentials, label removal, RBAC restoration/control-plane tampering, namespace
reuse, cleanup-only transitions and mixed-mutation denial, service failure, and
real controller recreation when present. Skip unsupported controller tests with
explicit remaining blockers. No fixed sleep proves propagation or settlement.
Keep namespace tombstones through cleanup; do not provide a blanket uninstall
that reopens previously quarantined writers. Roll back service code while
preserving policy, enrollment and TLS protection.

The metadata exceptions follow Kubernetes preparation before validating admission:
[Job strategy](https://github.com/kubernetes/kubernetes/blob/v1.30.0/pkg/registry/batch/job/strategy.go),
[Deployment strategy](https://github.com/kubernetes/kubernetes/blob/v1.30.0/pkg/registry/apps/deployment/strategy.go),
[custom-resource strategy](https://github.com/kubernetes/kubernetes/blob/v1.30.0/staging/src/k8s.io/apiextensions-apiserver/pkg/registry/customresource/strategy.go),
[managed-fields updater](https://github.com/kubernetes/apimachinery/blob/v0.30.0/pkg/util/managedfields/internal/managedfieldsupdater.go),
and [ObjectMeta schema](https://github.com/kubernetes/apimachinery/blob/v0.30.0/pkg/apis/meta/v1/types.go).

References: Kubernetes [dynamic admission](https://kubernetes.io/docs/reference/access-authn-authz/extensible-admission-controllers/)
and [validating policies](https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/).
