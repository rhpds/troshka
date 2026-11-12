# Native quota quarantine implementation plan

> **For agentic workers:** Use subagent-driven-development with a fresh implementer
> and task review per task, then a whole-phase review. Continue through local
> completion; stop before deployment, as explicitly requested by the user.

**Goal:** Build and validate an inactive server-side quarantine slice ready for isolated deployment acceptance.

**Architecture:** Pure enrollment and decision modules feed a separate HTTPS admission
process. An offline renderer scopes its webhook to exact enrolled names and adds
API-local lifetime and control-plane protections. No positive grants or settlement.

**Tech Stack:** Python 3.11-compatible operator code, existing pinned aiohttp/PyYAML,
pytest, admissionregistration.k8s.io/v1 resources.

**Spec:** docs/superpowers/specs/2026-11-12-native-quota-quarantine-design.md

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

### Task 1: Strict enrollment, cleanup classifier and bounded HTTPS service

**Files:** Create src/operator/admission/{__init__,models,quarantine,server}.py;
create src/operator/tests/test_quota_quarantine.py and test_quota_quarantine_server.py.
Promote the existing locked aiohttp version to direct requirements only if needed;
do not upgrade dependencies. Existing Dockerfile COPY already packages the modules.

**Interfaces:** models.load_enrollments(path) returns tuple[Enrollment, ...];
models.parse_enrollments(value) validates the same in-memory document.
quarantine.review(document, enrollments) returns a v1 AdmissionReview response;
malformed envelope without a usable request UID raises a safe validation error.
server.create_app(enrollments) returns aiohttp.web.Application; CLI module entrypoint
requires --config, --tls-cert, --tls-key and binds port 8443 by default.

- [ ] Write failing tests using synthetic full UUID enrollment and actual operator
  manifest builders. Example behavioral contract (fixture supplies a valid review):

  ```python
  def test_create_is_denied(review_for, enrollments):
      result = review(review_for("CREATE", "pods"), enrollments)
      assert result["response"]["allowed"] is False
  ```

- [ ] Run python3 pytest for both new modules and capture missing implementation RED.
- [ ] Implement immutable strict enrollment objects and exact duplicate/conflict
  checks. Test missing/full versus prefix identity, empty/oversize bundle, invalid
  namespace, extra keys and changed mode. No real project/credential fixtures.
- [ ] Implement exact cleanup comparisons from the spec. Test each permitted delta
  independently and paired with hidden growth/metadata/unknown-field mutations.
  Test UID changes, old/new name mismatch, scale objects, VM running/runStrategy
  exclusivity, malformed resource tuples, CREATE/CONNECT on unknown CRs and status
  updates on custom GVRs. Un-enrolled direct calls return a bounded denied reason;
  they must never become an enrollment API.
- [ ] Add HTTP tests for valid deny/cleanup, duplicate keys, oversized/deep JSON,
  malformed envelopes, disabled access-body logging, timeout and readiness. Use
  ephemeral loopback TLS only for local serving tests; never create cluster creds.
- [ ] Run new tests plus shared renderer/operator regressions. Assert no import of
  quota_fence, Kubernetes clients, backend configuration or ledger paths. Commit
  the task after required hooks; report exact validation and deliberate limitations.

### Task 2: Explicit offline bundle and isolated deployment acceptance

**Files:** Create src/operator/admission/{bundle,bundle_policy}.py;
scripts/render_native_quarantine.py; src/operator/tests/test_quota_quarantine_bundle.py;
docs/dev/native-quota-quarantine.md; synthetic JSON under
src/operator/tests/fixtures/quota_quarantine/ if useful.

**Interfaces:** bundle.render_bundle(enrollments, *, control_namespace,
control_namespace_uid, image, tls_secret, ca_pem, bootstrap_users,
controller_users, scheduler_nodes) returns staged Kubernetes resource documents.
The explicit existing control UID pins namespace lifetime; exact verified node
names constrain Pod binding and node affinity. CEL construction is a separate
focused module. VAP/binding protection itself requires effective RBAC because
Kubernetes forbids VAP self-matching; no second admission technology is added.
The CLI requires explicit input/output and every security-relevant argument;
it only validates and writes an operator-owned directory with a content manifest.
Use task1 parse_enrollments without silently changing its schema.

- [ ] Write failing renderer tests demonstrating exact scoping and fail-closed mode:

  ```python
  def test_webhook_never_selects_by_mutable_label(bundle):
      hook = bundle.webhook["webhooks"][0]
      assert "namespaceSelector" not in hook
      assert "objectSelector" not in hook
      assert hook["failurePolicy"] == "Fail"
      assert "DELETE" not in hook["rules"][0]["operations"]
  ```

- [ ] Implement exact-name API-local Namespace CREATE tombstones and UID checks,
  plus control namespace/resource isolation. Resolve legitimate Deployment
  controller/garbage-collector writes with narrow object+operation constraints;
  never exempt all controller/user mutations or leave a bootstrap-token escape.
  Escalate any isolation ambiguity before inventing a safe principal.
- [ ] Render staged prerequisites and activation. Require explicit named immutable
  image release and CA input, token automount false, immutable config, readiness,
  limits, bounded timeout and static exact scope. Never modify existing Helm or
  operator install kustomization. Render no token, private key or RBAC mutation grant.
- [ ] Test policy structure against create/update/delete namespace lifetime cases,
  malformed CEL-injection names/principals, empty/wildcard/mutable image input,
  deterministic output, safe permissions, no subprocess/network calls, overwrite
  refusal, and no changes to app quota flags or source fixtures. If a real local
  CEL compiler is unavailable, document syntax/type validation as a live gate.
- [ ] Require effective-permission checks before TLS installation: existing operator
  cluster-wide Secret reads, Rook nodes/proxy, bind/self-restoration, impersonation
  and trusted-controller/bootstrap credential access are explicit production
  blockers. Document a separate non-applied hardening proposal without modifying
  existing role manifests. Synthetic isolated-writer tests are not production
  identity isolation proof.
- [ ] Write copyable isolated acceptance steps: explicit kubeconfig; fresh target
  namespace and full CR/UID verification; pre-existing TLS with correct SAN/CA;
  separate preview, server dry-run, policy/control rollout and activation; exact
  expected denials, unrelated namespace success, service-outage behavior and
  namespace recreation denial. No automatic apply/cleanup or credential export.
- [ ] Include rollback preserving tombstones and DELETE access. Clearly distinguish
  namespace quarantine from stopped compute, settled intents, backend absence and
  quota readiness. Cross-namespace/direct storage paths remain unsupported.
- [ ] Run all quarantine tests, affected operator/backend suites, CLI help and
  synthetic render; inspect generated YAML. Commit after required hooks and report
  the first deployment-dependent gate without performing it.

## Phase completion

Run whole-phase review across these two tasks, fix findings with focused tests,
and verify clean committed state. Retain the parent native plan and its open
Task5b/6/7/8 gates. Stop before cluster deployment; no native enablement claim.
