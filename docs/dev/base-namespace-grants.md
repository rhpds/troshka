# Base namespace grants mode

The backend and operator can use two independently administered base namespace
RoleBindings instead of creating them. This setting covers only
`troshka-provider-namespaced` and `troshka-operator-namespaced`. It does not
restrict effective permissions or enable native quotas.

## Deployment configuration

The backend uses deployment-owned Dynaconf configuration:

```yaml
kubevirt:
  base_namespace_grants_mode: external
```

The environment override is
`TROSHKA_KUBEVIRT__BASE_NAMESPACE_GRANTS_MODE=external`. The operator independently
uses `TROSHKA_BASE_NAMESPACE_GRANTS_MODE=external` in its Deployment environment.
Configure both processes consistently and restart them through a separately
authorized rollout. Setting one does not set or verify the other.

Only the exact strings `managed` and `external` are supported. Absence defaults
to `managed`; explicit empty/null/boolean/unknown/mixed-case/whitespace values
fail. The mode never comes from a project CR, topology, request or provider
credential record. Default manifests continue to use managed behavior.

## Administrator prerequisites

Before selecting external mode, independently prepare every applicable namespace
and both exact RoleBindings, including the backend's `troshka`, `troshka-cache`
and configured operator namespace. Each binding must reference the corresponding
ClusterRole in `rbac.authorization.k8s.io` with exactly these subjects:

| Binding and ClusterRole | ServiceAccount namespace/name |
| --- | --- |
| `troshka-provider-namespaced` | `troshka/troshka` |
| `troshka-operator-namespaced` | `troshka-operator/troshka-operator` |

These are the existing backend identities. The operator helper retains its
existing optional `operator_namespace` argument, default `troshka-operator`.
This change does not reconcile a custom operator namespace with the backend's
fixed subject namespace. Preserve and verify the expected identities for each
caller rather than inferring new subjects.

Administrator-owned cluster roles/bindings and the independent operator
installation must already exist. External mode rejects provider-driven operator
installation/reinstallation at entry, before bootstrap writes. Consequently new
virtual-host registration through `provision_host` is unsupported in external
mode until a separate independently administered bootstrap exists. Existing
registered hosts can use independently prepared project namespaces and grants.
Creating fresh project namespaces is also unavailable in this mode.

## Validation and compatibility

External mode reads both named RoleBindings and verifies their namespace/name,
nondeleting state, exact roleRef and complete subject collection. Extra or
duplicate subjects, a wrong ServiceAccount namespace, or a noncore subject
apiGroup fail. The API's omitted/empty core ServiceAccount apiGroup representations
are equivalent. Missing/forbidden resources, transport failures and unverified
responses stop the path without repair or response-body disclosure.

The backend project namespace helper first reads the existing namespace,
rejects a changed/deleting namespace, validates both grants and returns without
namespace CREATE or label PATCH. Later project secret/cache/CR operations cannot
continue on validation failure. `deploy_project` can perform its existing CRD
reconciliation before this helper: the whole deployment operation is not
guaranteed write-free. External provider RBAC bootstrap reads the three system
namespaces and grants, verifies administrator-owned cluster-role/binding
existence, then returns without Namespace/ServiceAccount manifest application.

Operator project and VM create handlers preflight before status staging,
capture/deploy work and supplemental grants. VM CPU normalization remains a
read/deep-copy operation and now follows external preflight. Managed mode keeps
the legacy create/409 behavior and original ordering, including best-effort
continuation on ordinary binding API errors. Invalid configuration and explicit
external verification errors propagate rather than entering those warning paths.

## Limits and rollback

Successful reads prove the observed binding shape only. They do not prove role
rules, effective permissions, namespace ownership or lifetime, absence of other
grants, or protection against later edits. This mode intentionally permits
successful creates to reach existing supplementary grant writers.

BMC, VNC, SCC/exec, recert/guestfish, Ceph/Rook, provider bootstrap outside these
base grant checks, other handler routes, startup/timer recovery and Kubernetes
controllers retain their separate behavior. Global RBAC/bind privileges, global
Secret access, OVN exec, direct storage and alternate credential paths remain
separate isolation blockers. No general externally managed RBAC profile or
writer fence follows from this setting.

Rollback explicitly sets both deployments back to `managed` and restarts them
through a separately authorized rollout. This restores automatic base-binding
creation; do not automate it in response to intentional grant revocation. This
local code change deletes/transfers no assets, namespaces, ownership or history.

Full Task 5b mediation/authority retirement, credential and controller isolation,
API routing, accepted-request drain, durable fence acknowledgement and settlement
remain unproved. Task 6 stop/release evidence and later native quota enablement
gates remain mandatory. No live rollout or quota enablement is authorized here.

## Validation record — 2026-11-12

Implementation `601254c3` passed independent scoped review, 314 backend tests and
1,661 operator tests. Seven existing local HTTP-server tests initially could not
bind loopback sockets in the restricted sandbox; the permitted loopback rerun
passed the entire operator suite. No cluster calls were part of those suites.

A subsequent read-only check used the reviewed backend validator in external
mode against the existing `troshka-cache` namespace on ocpvdev01. GET-only client
adapters used bounded request timeouts. Namespace and both binding identities
and resource versions were identical before and after validation:

| Object | UID | Resource version |
| --- | --- | --- |
| Namespace `troshka-cache` | `adad19a0-d126-4266-8477-2f40e8eb6b26` | `2088741334` |
| RoleBinding `troshka-provider-namespaced` | `f99e2c0d-266e-4dda-ace0-32021f371288` | `2462089332` |
| RoleBinding `troshka-operator-namespaced` | `c20c36d7-5bf6-442e-9f3e-e2768e07509c` | `2462089335` |

Validation completed successfully with no API writes or deployed configuration
changes. The development kubeconfig disables TLS verification; warnings were
retained. This checks real API response compatibility only, not authenticated
production isolation, actual writer authorization or deployed handler behavior.
No grants were revoked, no workload restarted and no quotas enabled.
