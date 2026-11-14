# Online native project enrollment

User-approved requirement: implement compatibility without stopping existing VMs.
Production promotion remains held until dev acceptance proves compatibility.

## Invariants

- Enrollment performs provider reads only. No VM stop/start, CR patches, disk
  writes, namespace deletion, quota enablement, or ownership reassignment.
- Record explicit administrator-authorized custody, never fabricated historical
  creation, dispatch, or stop proofs.
- Bind database owner/project/provider, cluster/namespace/CR UIDs, retained
  topology, and independently verified storage identities. Reject changed,
  ambiguous, stale, incomplete, or foreign observations.
- Cleanup remains owner-requested and uses confirmed off proof, conditional
  object deletion, and exact private disk/trash settlement. No shared trash purge.
- Keep existing start/stop behavior and disable automatic quota enforcement for
  adopted allocations until its separate readiness gates pass.

## Tasks

1. Implement explicit online ownership enrollment and durable receipt with tests
   proving no provider mutations and rejection of identity changes.
2. Integrate receipts into cleanup/accounting without bypassing retained-context,
   storage graph, shutdown, or settlement checks; test full legacy lifecycle.
3. Expose a protected operator CLI using the existing read-only enrollment
   workflow; document preview/apply and retry behavior.
4. Review security and compatibility; run focused suites and required checks.
5. Apply dev overlay, enroll an actual legacy API-created running project, verify
   unchanged VM UID/power and guest data, then test owner-requested cleanup through
   the Troshka API and exact private disk/trash absence.
6. Produce normal images and repeat release verification. Only then resume the
   authorized dev-first production rollout with preserved existing projects.

## Progress

- Production baseline: four native deployed projects have no quota reservations.
  Current managed cleanup rejects these; no existing importer fixes that online.
- Work branch: codex/online-native-enrollment, based on 5a2e8b6f.
- Ruling: online means enrollment never changes provider resources; shutdown is
  deferred to a later explicit lifecycle request.
- Core receipt/CLI implementation is in dev as a temporary immutable overlay.
  Live verification safely rejected legacy unowned console/S3 support objects;
  no custody has yet been applied. Test VM stayed running with unchanged VMI UID.
- User approved continuing compatibility for existing support objects, cluster
  groups, BMC, Ceph appliances and VM persistent-state disks. Implement exact
  identity/content validation and cleanup-only support; preserve quota admission
  restrictions and require real producer shutdown before cleanup.
- Troshkad/libvirt deployments do not use Kubernetes owner references. Preserve
  their existing cleanup routing and include regression coverage.
- Compatibility implementation passes 247 operator tests and a troshkad routing
  regression. Fresh review is in progress. Live read-only validation passes the
  dev VM namespace; four production namespaces currently reject unowned ops or
  workload-runner pods. Investigate their exact identity and terminal state
  before extending recognition. No production mutations have been made.
- PostgreSQL enrollment run: 66 passed, 16 fixture errors from future source
  timestamps. Resolve the fixture clock mismatch without weakening production
  timestamp validation, then repeat the focused suite.
- Dev acceptance project remains active through the Troshka API. Compatibility
  overlay is staged locally only; it must verify source hashes on every replica.
- Fixture timestamp corrected to use the shared fixed test clock; focused
  PostgreSQL-backed suite now passes (82 tests, no skips).
- Read-only production diagnosis: two ops pods remain Running; two workload-runner
  pods are Succeeded. Both profiles remain rejected. Legacy COW ancestry also
  needs current snapshot/intermediate identity proof beyond CSI-to-RBD binding.
  User decision requested before expanding either compatibility path. No new
  overlay or production deployment was applied.
- User approved completing both remaining compatibility paths: independently
  verified current COW ancestry and legacy ops/workload-runner custody. Enrollment
  stays provider-read-only; writer termination is limited to a later explicit
  owner Delete/Undeploy. Do not infer private storage ownership from names alone.
- Current-ancestry implementation is frozen for independent review: CSI journal,
  immutable parent/snapshot identities, exclusive intermediate descendants, and
  independently bound cache source. Existing CSI deletion and exact trash path
  retained. Focused PostgreSQL suite passes 84 tests, including a slow verification
  scan followed by the unchanged 60-second apply freshness gate.
- Read-only dev evidence confirms head `6a9c1c3c5f4761`, intermediate
  `a9c1c35db74166`, and shared source `a9c1c3137b6b90`. Intermediate has no separate
  PVC metadata; its association is verified jointly through CSI journal and exact
  snapshot/child relationships, never from its name alone.
- Ancestry independent review passed (original missing-ancestry finding resolved).
- Writer review fix round 1: existing ops healer calls `_recreate_ops_pod`
  directly, bypassing the new serialized launch guard. Extend the same lock and
  cleanup-pending guard to actual healing/recreation paths, with race regression
  and scoped re-review before dev mutation.
- Writer fix round 1 passed scoped review: launch and the whole healer operation
  share the project lock and pending-cleanup guard. Final namespace pins are also
  rechecked after ancestry scanning. The final PostgreSQL enrollment/CLI run
  passes 86 tests with no skips; historical network matcher review and live dev
  acceptance remain pending.
- Final legacy matcher implementation is frozen. All five read-only namespace
  profiles pass: dev 32 pins; production 85, 77, 95, and 94 pins. Operator tests
  pass 271 and provider/healer tests pass 55; final matcher review is in progress.
  No provider resources were changed to obtain compatibility.
- Final matcher review fix round 1: staged cleanup must validate sealed custody
  independently of already removed root/network/gateway dependencies, and must
  conditionally delete custody Services/Routes instead of preserving them as
  bootstrap objects that block namespace readiness. Dev overlay remains held
  until these two integration findings and regressions pass scoped review.
- Both staged cleanup findings passed scoped re-review. Validation: 276 operator
  tests, 6 backend readiness cases, and 86 PostgreSQL enrollment/CLI tests pass.
  The dev compatibility overlay is applied and verified on all seven replicas
  across six deployments, including operator. Collection jobs finished before
  workers restarted; the dev scheduler is restored. Fresh online enrollment of
  the existing dev acceptance VM is in progress; production remains untouched.
- Live enrollment succeeded after explicitly adding the real `troshka-cache`
  namespace with existing additive cache authorization. Mixed managed/legacy
  source retention fix passed scoped review and 59 PostgreSQL-enabled tests.
  Receipt `1fa2a367-64b8-41d5-b458-188b09696c23` records current custody, with
  zero reservations and no provider mutations. VMI UID/creation time, guest boot
  ID and disk checksum are unchanged. All dev ownership and asset inventory
  hashes match the baseline. Scheduler restored; API lifecycle acceptance next.
- Live API lifecycle acceptance passed: ordinary Stop/Start preserved the guest
  file checksum, and Undeploy finalized to draft with no deployment error.
  Settlement report `21a00a24-b43b-4947-8a18-f5b6c3e4bf48` proves private head
  `6a9c1c3c5f4761` and intermediate `a9c1c35db74166` absent from active storage
  and trash. Shared source `a9c1c3137b6b90` remains present and completely measured;
  its obsolete snapshot/child edges are gone. The transient shared graph failure
  during deletion recovered on the next collection without a code change.
- Final dev inventory preserves all owners, unrelated project hashes, and every
  image/pattern/library hash. The test project's owner and canvas are unchanged;
  only its intentional deployment lifecycle changed. Quotas remain disabled.
- Quota usage now translates cleanup-only AccountingUnavailable to authorized,
  generic HTTP 503 rather than HTTP 500 or fabricated zero usage. All 13 usage
  API tests pass, including all scopes and unauthorized access. Scoped independent
  review found no issues. A backend-only immutable dev overlay passed rollout,
  runtime hash/import and final API checks; worker deployments were not restarted.
- Compatibility development and dev acceptance are complete. Normal image
  packaging, release validation, and fresh production ancestry/enrollment remain
  promotion steps. Production has not been mutated by this compatibility work.
