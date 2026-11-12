# Native quota quarantine — isolated acceptance procedure

## Description of entry point

Use this procedure to validate the inactive quarantine admission service against a
fresh, disposable namespace on an isolated Kubernetes 1.30+ acceptance cluster.
Local renderer/tests are complete only when reported separately; they are not
evidence that API-server CEL, TLS, controllers or production isolation work.
**Stop before deployment unless that deployment has been explicitly authorized.**
This procedure contains deliberate write commands for a later acceptance operator;
the renderer never executes them. Do not run the blocks as an unattended script.

Quarantine denies namespace growth; it does not stop existing compute, settle
intents, prove backend absence, free capacity, or make native quotas ready. Existing
provider/BMC credentials and independent controllers must be tested separately.
Cross-namespace DataVolume/cache/clone paths, direct storage access and full writer
mediation remain unsupported. Parent native Tasks 5b, 6, 7 and 8 remain open.

## Prerequisites and authority gates

Use `python3` with the operator's locked dependencies, `oc`, `jq`, `openssl`, and
an explicit acceptance kubeconfig. No GWS/MCP authentication is involved. This
tool has no Kubernetes client, credential discovery, subprocess or network path.
It needs an existing control namespace UID, existing project namespace UID and
full project-root CR UID, a named immutable release tag, a CA certificate, and
independently verified identities/node names. It generates no Namespace, TLS
Secret, token, Role, RoleBinding or cluster RBAC grant.

Before **any TLS installation**, a target-specific effective-authority audit must
prove old provider/operator/BMC/Rook writers cannot:

- Get/list/watch control TLS Secrets or bootstrap/controller Secrets; admission
  cannot intercept reads. Existing `src/operator/deploy/clusterrole.yaml` has
  cluster-wide Secret reads: this is a production blocker.
- Create Pods or TokenRequests in trusted-controller/bootstrap namespaces, obtain
  or impersonate an allowed username, or reuse previously exposed credentials.
- Use `nodes/proxy`, global Pod exec, privileged node execution or an equivalent
  path to steal control credentials. Existing Rook roles include `nodes/proxy`.
- Create/modify bindings or role definitions/aggregation to regain those powers.
  Removing explicit `bind` alone is insufficient when equivalent permissions
  remain. Existing operator binding restoration must be addressed separately.
- Modify the webhook, policy or binding, or steal/restore authority to do so.
  **VAP cannot match ValidatingAdmissionPolicy/Binding resources themselves.**
  Their protection is an administrative RBAC prerequisite, not rendered
  self-protection. The rendered webhook-configuration policy is an additional
  mutation check, not a replacement for the authority audit.

A separate, **non-applied** hardening proposal: split operator/Rook global Secret
and workload privileges into exact project/cache RoleBindings; remove unnecessary
node proxy/global exec; constrain grant creation/restoration and referenced role
rules; update reconcilers so they do not restore broad grants; retire exposed TLS
and controller/bootstrap credentials. Preserve existing role objects and review
compatibility/rollback before any change. This bundle changes none of those roles.
Record effective existing bindings and tests using actual old writer credentials,
not just fresh synthetic accounts. Never print Secret values. Authorization
checks alone do not exercise admission or prove previous watches/tokens drained.

The isolated synthetic writer fixture deliberately lacks those inherited global
privileges. Passing it proves routing/classification only. Production enrollment
remains blocked until the real authority graph and credential retirement pass.

## Procedure

### 1. Establish a fresh isolated target and review identities

After explicit deployment authorization, choose values and run read-only checks:

```bash
export ACCEPTANCE_KUBECONFIG=/absolute/path/isolated-acceptance.kubeconfig
export OLD_WRITER_KUBECONFIG=/absolute/path/isolated-old-writer.kubeconfig
export CONTROL_NS=quarantine-control
export TARGET_NS=quarantine-fixture
export OTHER_NS=quarantine-unenrolled
export PROJECT_ROOT_NS=troshka-operator
export PROJECT_CR=quarantine-fixture
export PROJECT_ID='<full-project-UUID>'
export TLS_SECRET=quarantine-tls
export ADMISSION_IMAGE='quay.io/<organization>/troshka-operator:<named-release-tag>'
export INPUT_DIR=/absolute/private/path/quarantine-inputs
export BUNDLE_DIR=/absolute/private/path/quarantine-first-install
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" version -o yaml
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" api-resources --api-group=admissionregistration.k8s.io
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" get validatingadmissionpolicies,validatingadmissionpolicybindings,validatingwebhookconfigurations -o name
```

Require the v1 VAP/binding APIs and webhook matchConditions. If any
`troshka-quarantine*` installation already exists, or there is nonempty tombstone
history, **stop this first-install procedure**. Fixed resource names support one
installation per cluster. Offline rendering cannot discover prior lifetimes;
blind apply/rerender could erase tombstones. Updates require independent cumulative
history reconciliation and review. Never remove an old enrollment to make a render
succeed; the maximum supported inventory is 32. Do not weaken policies as fallback.

Create fresh target/unrelated/control namespaces only as separately authorized
fixture setup. An existing production namespace is not an acceptance fixture.
Seed a synthetic TroshkaProject through the usual controlled fixture procedure;
verify its full `metadata.uid`, project UUID and namespace ownership against the
current source of truth. A namespace label alone is insufficient. Record the
writer-maintenance boundary so no in-flight writer can race enrollment evidence.

```bash
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" get namespace kube-system "$CONTROL_NS" "$TARGET_NS" -o json \
  | jq '.items[] | {name:.metadata.name,uid:.metadata.uid}'
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" -n "$PROJECT_ROOT_NS" get troshkaproject "$PROJECT_CR" -o json \
  | jq '{name:.metadata.name,uid:.metadata.uid,spec:.spec}'
```

Verify root ownership/full UUID using the real schema, not a guessed field mapping.
Also verify nodes are isolated eligible workers and audit actual request usernames
for deployment, replicaset, scheduler, pod_status, endpoints, endpointslice,
garbage_collection and disruption controllers. Supply exact usernames; there are
no defaults, group/prefix exceptions, or automatic discovery. `pod_status` includes
only verified reporters on the selected nodes. Shared controller usernames may
have a union of named roles, but bootstrap/controller overlap is rejected.
If audit evidence is unavailable, stop; do not guess `system:*` identities.

For each actual old writer credential, inspect authorization at both the control
namespace and every trusted credential namespace, plus cluster-scoped powers:

```bash
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" auth can-i --list -n "$CONTROL_NS"
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" auth can-i get secrets -n "$CONTROL_NS"
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" auth can-i list secrets -n "$CONTROL_NS"
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" auth can-i watch secrets -n "$CONTROL_NS"
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" auth can-i create serviceaccounts/token -n "$CONTROL_NS"
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" auth can-i get nodes/proxy
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" auth can-i impersonate users
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" auth can-i create clusterrolebindings
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" auth can-i bind clusterroles
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" auth can-i update validatingadmissionpolicies
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" auth can-i update validatingadmissionpolicybindings
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" auth can-i update validatingwebhookconfigurations
```

Expected sensitive permissions: `no`, with effective grant review accounting for
wildcards, named-resource grants, alternate verbs, RoleBinding creation, escalation
and controller credentials. Repeat token/Pod/Secret checks in all trusted
namespaces. An API/DNS/RBAC discovery error is unverified, not a successful denial.

### 2. Prepare explicit offline inputs and preview

In a private input directory, create `enrollments.json` using the exact schema;
replace every placeholder with independently verified full values:

```json
{"schema_version":1,"enrollments":[{
  "mode":"quarantine",
  "cluster_key":"<kube-system-Namespace-UUID>",
  "enrollment_id":"<new-full-enrollment-UUID>",
  "namespace_name":"quarantine-fixture",
  "namespace_uid":"<target-Namespace-UUID>",
  "project_id":"<full-project-UUID>",
  "project_cr_name":"quarantine-fixture",
  "project_cr_uid":"<full-TroshkaProject-UUID>"
}]}
```

Create `bootstrap-users.json` as a JSON array of verified administrator usernames,
`scheduler-nodes.json` as an array of exact verified node names, and
`controller-users.json` with all eight keys (each value a nonempty exact-username
array). These illustrative names are **not deployable identity defaults**:

```json
{"deployment":["<verified-user>"],"replicaset":["<verified-user>"],
 "scheduler":["<verified-user>"],"pod_status":["<verified-user>"],
 "endpoints":["<verified-user>"],"endpointslice":["<verified-user>"],
 "garbage_collection":["<verified-user>"],"disruption":["<verified-user>"]}
```

Obtain public `ca.pem` and the issued **public certificate** `server.crt` from the
platform certificate procedure without exporting any private key or token. Require
the existing TLS Secret name and keys `tls.crt`/`tls.key`, SAN
`troshka-quarantine.${CONTROL_NS}.svc`, matching CA, validity interval and mounted
key readability by the nonroot workload. TLS installation occurs only after the
authority audit and API-local protections below; do not create a TLS Secret from
this runbook. Check the certificate locally:

```bash
openssl verify -CAfile "$INPUT_DIR/ca.pem" \
  -verify_hostname "troshka-quarantine.${CONTROL_NS}.svc" "$INPUT_DIR/server.crt"
export CONTROL_UID='<verified-existing-control-Namespace-UUID>'
python3 scripts/render_native_quarantine.py \
  --enrollments "$INPUT_DIR/enrollments.json" --output "$BUNDLE_DIR" \
  --control-namespace "$CONTROL_NS" --control-namespace-uid "$CONTROL_UID" \
  --image "$ADMISSION_IMAGE" --tls-secret "$TLS_SECRET" --ca-file "$INPUT_DIR/ca.pem" \
  --bootstrap-users-file "$INPUT_DIR/bootstrap-users.json" \
  --controller-users-file "$INPUT_DIR/controller-users.json" \
  --scheduler-nodes-file "$INPUT_DIR/scheduler-nodes.json"
cat "$BUNDLE_DIR/manifest.json"
cat "$BUNDLE_DIR/00-protections.yaml" "$BUNDLE_DIR/10-workload.yaml" "$BUNDLE_DIR/20-activation.yaml"
```

Expected: three staged YAML files plus SHA256 content manifest; directory `0700`,
files `0600`. Existing output/symlinks reject. Inputs stay unchanged. No private
key or credential is rendered. Check the registry release exists, is immutable
under registry policy, and packages the admission module. A version-looking tag
cannot prove registry immutability offline. `latest/main/production`, digest-only,
empty and wildcard references reject.

### 3. Server validation and policy rollout — first deployment-dependent gate

**This is the first API validation/write boundary after local completion.** Recheck
all Namespace/CR UIDs and absence of existing installation immediately before it.
Local tests inspect structure; no local CEL compiler/type checker was available.
YAML parsing is not CEL validation. Server dry-run must succeed with no expression
warnings; installation must then report no `status.typeChecking.expressionWarnings`.

```bash
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" apply --dry-run=server -f "$BUNDLE_DIR/00-protections.yaml"
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" apply --dry-run=server -f "$BUNDLE_DIR/10-workload.yaml"
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" apply --dry-run=server -f "$BUNDLE_DIR/20-activation.yaml"
```

Review each stage; only after the explicit deployment decision:

```bash
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" apply -f "$BUNDLE_DIR/00-protections.yaml"
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" get validatingadmissionpolicies \
  troshka-quarantine-lifetimes troshka-quarantine-control troshka-quarantine-webhook -o json \
  | jq '.items[] | {name:.metadata.name,generation:.metadata.generation,status:.status}'
```

Require active Deny bindings, current policy status and exercised expected denials;
a fixed wait is not propagation proof. With the old writer, server-dry-run control
Secret/ConfigMap/Deployment/Service mutation, RoleBinding restoration, token
requests and Namespace DELETE: each must be denied. Authorized bootstrap control
Namespace DELETE is also denied; enrolled target DELETE remains available. Use
copies of synthetic manifests, never read/export a live Secret to make a test.

Only after authority gates and policy acceptance, have the platform procedure
install the pre-issued TLS Secret. Verify existence/type/key names without data:

```bash
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" -n "$CONTROL_NS" get secret "$TLS_SECRET" \
  -o go-template='{{.type}}{{"\n"}}{{range $k,$v := .data}}{{$k}}{{"\n"}}{{end}}'
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" apply -f "$BUNDLE_DIR/10-workload.yaml"
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" -n "$CONTROL_NS" rollout status deployment/troshka-quarantine --timeout=120s
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" -n "$CONTROL_NS" get pods,replicasets,endpoints,endpointslices,poddisruptionbudgets -o wide
```

Require two ready replicas on the explicit eligible nodes, correct image, token
automount false, HTTPS probes and healthy endpoints/disruption status. Inspect
audit identity and exact operation for every policy-denied controller request.
Never grant a blanket controller or bootstrap exemption to fix availability.

Controller scope: Deployment controller owns admission ReplicaSets and only the
Deployment status; ReplicaSet controller creates/deletes shaped admission Pods
and updates ReplicaSet status; scheduler only binds matching names to explicit
nodes; node/status identities only update Pod status; endpoint controllers only
publish the admission Service; disruption controller only updates its PDB status;
garbage collection only deletes matching Pods/ReplicaSets/EndpointSlices.
Token requests, exec/attach, root Pod edits, RBAC and desired root configuration
are denied to controllers. Bootstrap can manage exact named roots only.

**Trust limitation:** CEL cannot fetch parent objects to prove owner ancestry.
Names/labels/owner references are shape checks, with exact authenticated controller
identity as the primary boundary. Unknown descendant UIDs are not invented.
Controller credential theft, arbitrary controllers or guessed identities invalidate
this boundary. Node changes require reviewed rerender and reporter identity checks.

### 4. Seed fixtures, activate and exercise denials

Before activation, use separately authorized isolated writer permissions to seed
`cleanup-demo` Deployment (two replicas), a Pod and PVC in the target, plus any
supported Job/VM/VMI fixtures. Use no production disk or credential. Record actual
fixture names and existing owner UIDs. An old writer must have sufficient ordinary
mutation RBAC; an RBAC denial alone does not prove admission interception.

```bash
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" -n "$TARGET_NS" create deployment cleanup-demo \
  --image=registry.access.redhat.com/ubi9/ubi-minimal:9.5 --replicas=2 -- /bin/sh -c 'exec tail -f /dev/null'
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" apply -f "$BUNDLE_DIR/20-activation.yaml"
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" get validatingwebhookconfiguration troshka-quarantine -o yaml
```

Check exact namespace matchConditions, no label selectors, failurePolicy Fail,
3-second timeout and no DELETE rule. Then run these independently:

```bash
# DENY: reason growth_denied.
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" -n "$TARGET_NS" create configmap growth-probe --from-literal=x=y --dry-run=server
# SUCCESS: unrelated namespaces are filtered before any webhook call.
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" -n "$OTHER_NS" create configmap unrelated-probe --from-literal=x=y --dry-run=server
# SUCCESS: exact cleanup; inspect persisted replicas=0 afterward.
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" -n "$TARGET_NS" scale deployment/cleanup-demo --replicas=0
# DENY: growth, even by administrator or original controller credentials.
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" -n "$TARGET_NS" scale deployment/cleanup-demo --replicas=1 --dry-run=server
# DENY: cleanup mixed with template/image changes.
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" -n "$TARGET_NS" patch deployment cleanup-demo --type=merge --dry-run=server \
  -p '{"spec":{"replicas":0,"template":{"metadata":{"annotations":{"fixture":"growth"}}}}}'
# Namespace label mutation cannot opt out; rerun growth-probe after this succeeds.
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" label namespace "$TARGET_NS" quarantine-fixture=changed --overwrite
```

Record response operation/resource/subresource, reason and audit identity without
raw request bodies or credentials. For the fixture VM/Job, check `running:false`
or `runStrategy:Halted` (exactly one power mode), Job `suspend:true`, supported
status-only updates and removal-only finalizers on already-deleting objects.
Paired image/disk/label/owner/unknown-field mutations must deny. Unsupported
custom-controller status/finalizer changes are intentionally denied.

Exercise actual old BMC CONNECT actions (restart/start/console/portforward) and
KubeVirt aggregated subresources using the isolated fixture IDs. Verify every
request reaches the API routing boundary: aggregation may have different admission
behavior. If a path is not intercepted, mark native enforcement blocked; do not
claim coverage from synthetic HTTP reviews. Delete a disposable workload Pod and
observe the real controller's recreation being rejected in the enrolled namespace.
Skipped CDI/KubeVirt/controller paths remain explicit blockers.

### 5. Outage, DELETE and namespace lifetime acceptance

Using bootstrap credentials, root-patch the control Deployment to zero (its scale
subresource is intentionally not a bootstrap exception). Wait for confirmed Pod
termination/endpoints absence, then exercise growth and cleanup:

```bash
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" -n "$CONTROL_NS" patch deployment troshka-quarantine --type=merge -p '{"spec":{"replicas":0}}'
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" -n "$CONTROL_NS" wait --for=delete pod \
  -l app.kubernetes.io/name=troshka-quarantine --timeout=120s
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" -n "$TARGET_NS" create configmap outage-probe --from-literal=x=y --dry-run=server
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" -n "$TARGET_NS" scale deployment/cleanup-demo --replicas=0 --dry-run=server
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" -n "$OTHER_NS" create configmap unrelated-outage --from-literal=x=y --dry-run=server
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" -n "$TARGET_NS" delete deployment cleanup-demo --dry-run=server
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" -n "$CONTROL_NS" patch deployment troshka-quarantine --type=merge -p '{"spec":{"replicas":2}}'
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" -n "$CONTROL_NS" rollout status deployment/troshka-quarantine --timeout=120s
```

Expected: first two target non-DELETE requests fail closed with unavailable webhook;
unrelated CREATE and authorized DELETE succeed. Verify independent authorization
still applies. DELETE is outside the webhook; control namespace protection still
applies. Storage-preserving emergency compute stop needs verified VMI/Pod deletion
with correct owner/controller behavior; namespace/project deletion may destroy
storage and must never be described as a safe stop or a quota refund.

For the **disposable** fixture only, separately authorize deletion and confirm all
remaining contents are expendable. Delete it deliberately, keep every tombstone,
and demonstrate that the API-local CREATE denial survives service outage:

```bash
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" delete namespace "$TARGET_NS" --wait=false
oc --kubeconfig="$ACCEPTANCE_KUBECONFIG" wait --for=delete namespace/"$TARGET_NS" --timeout=120s
oc --kubeconfig="$OLD_WRITER_KUBECONFIG" create namespace "$TARGET_NS" --dry-run=server
```

Expected last command: `quarantine_control_denied` from lifetime policy. Repeat
with service unavailable and bootstrap credentials: CREATE must still deny.
If deletion stalls on unsupported finalizers/controllers, record the blocker and
stop; do not strip arbitrary finalizers or weaken the policies to force completion.
Verify UPDATE/DELETE with wrong lifetime UID fail using approved API test fixtures;
server-supplied UID immutability is not a reason to remove policy checks. Never
delete tombstone policy/binding to complete fixture cleanup.

## Troubleshooting and rollback

- `quarantine render failed`: validate schema/UUIDs, required arguments, named
  version tag/CA and a new output path. No partial render should be applied; on
  write failure preserve evidence and choose a new path after inspection.
- CEL warnings or controller rollout failure: stop before activation. Capture the
  exact denial/audit identity, fix only a reviewed narrow rule, rerender elsewhere
  and repeat validation. Do not add wildcard identity/operation exceptions.
- TLS/probe failure: check public SAN/chain/expiry, port 8443, workload readability
  and endpoint health. Never print/export private key data for debugging.
- Admission outage: non-DELETE cleanup also fails closed. Use independently
  reviewed storage-preserving stop paths and restore the admission service.

Rollback service code to a known named release only with a matching reviewed
control policy image constraint and unchanged enrollment/TLS protection. Preserve
all API-local Namespace tombstones, Deny bindings, exact webhook scope and
failurePolicy Fail. Keep DELETE authorization available. Do not remove the webhook
as an availability fix or perform a blanket uninstall: that reopens old writers.
The control namespace cannot be deleted even by bootstrap while this policy is
active. Record unresolved writer drain/storage/settlement gates after rollback.

## Reference

- [Quarantine design](../superpowers/specs/2026-11-12-native-quota-quarantine-design.md)
- [Implementation plan](../superpowers/plans/2026-11-12-native-quota-quarantine.md)
- [Kubernetes VAP API: self-matching restrictions](https://kubernetes.io/docs/reference/kubernetes-api/admissionregistration/validating-admission-policy-v1/)
- [Dynamic admission and matchConditions](https://kubernetes.io/docs/reference/access-authn-authz/extensible-admission-controllers/)
- [RBAC escalation and binding restrictions](https://kubernetes.io/docs/reference/access-authn-authz/rbac/#restrictions-on-role-binding-creation-or-update)
