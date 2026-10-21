# Build, capture, validate, promote, and resume

Use current supported platform tools, selected framework config, and provider
collection. Verify API schemas/module contracts before issuing requests. These
procedures do not grant authorization for orders, captures, deletion, publication,
or external writes; use the scope already supplied by the developer.

## Track progress outside Git

Use [the state example](../assets/migration-state.example.json) in a
developer-selected external location. Record source revisions and dirty-file
fingerprints, generated-artifact fingerprints, resource identifiers, stage
outcomes, redacted evidence locations, and blockers. Exclude secrets, credentialed
URLs, access tokens, kubeconfigs, full provision data, and decrypted variables.

On resume, compare inputs/fingerprints and reconcile actual project, allocation,
job, and pattern state. Source changes invalidate dependent assessment and
validation; do not discard unrelated evidence without a reason. A timeout or
permission failure means unknown. Do not create another build, repeat capture,
release an allocation, or delete a resource on the assumption it is absent.
Historical state is neither live truth nor transferable permission.

## Dedicated development build

Validate candidate artifacts statically, then order the dev CI in template mode
through the existing catalog flow, or build the standalone template through
supported Troshka tools when that is the authorized request. Record which route
was exercised; direct template success does not verify catalog integration.

Confirm infrastructure readiness, ordered workload execution, resulting services,
actual runtime revision, user outputs, Showroom behavior, and TLS trust. Verify
that exactly the assigned executor ran each phase. Preserve failure evidence;
correct the owning configuration before rerunning affected stages. Do not reset
completion markers or rerun destructive initialization just to clear a failure.

## Capture owned infrastructure

Capture only after the dev environment meets its build acceptance checks.
Inspect the selected version's pattern capture source/API and the framework's
post-software behavior. Some dev configurations enable
`troshka_capture_on_complete`; avoid a second manual capture of that same build.
Capture can quiesce/restart workloads; verify the configured behavior and scope.
Do not capture while a workload chain or content update is still running.

Record returned pattern ID, state, source project, topology/disk identities,
available checksums, capture completion, and target-backend disk availability.
An accepted capture request does not prove disks are ready. Names and timestamps
are labels, not content guarantees. Inspect whether the ID can be recaptured or
its backing disks changed. If immutable release identity cannot be guaranteed,
record and compare content evidence before promotion; block promotion if the
tested artifact cannot be established as the deployed one. Do not invent a
`troshka_pattern_version` field or claim a UUID guarantees immutability.

Never capture a shared tenant's cluster. For hybrid CIs, independently owned
access VMs may have a capture path, but shared cluster credentials/identities
must not be baked into those disks.

## Fresh pattern validation

Deploy the selected pattern into a separate isolated project with fresh
deployment credentials. Establish actual supported refresh behavior; changing
`common_password` in the request alone does not prove every guest/service changed.
Check:

- Services and workloads function without source-project state.
- Internal stable names work independently in both environments.
- Runtime passwords, keys, tokens, certificates, and registrations follow the
  intended reuse/regeneration rules, and old credentials do not retain access.
- Showroom links/terminals/consoles target the restored project; inspect login,
  redirects, and credentials rather than only HTTP status.
- TLS trust works without blanket verification bypass and without the original
  external certificate issuer or DNS zone, except documented lesson dependencies.
- Post-restore work runs once or safely retries; unknown external registration
  outcomes are reconciled before replay. Avoid duplicate registrations.
- Supported stop/start works and independently authorized test destruction
  leaves the source project and other deployments intact.

Source-only or offline evidence cannot satisfy these runtime checks.

## Shared-tenant validation and release

Use the existing allocator and workload framework; pin their revisions. Check
whether Troshka can represent the allocation and access entry point through
supported interfaces. Current project-local cluster targeting is not evidence
of arbitrary external cluster attachment. If unsupported, stop that integration
path and deliver a partial/native-tenant candidate rather than deploying a cluster.

When authorized and supported, provision two tenants on the supplied shared
cluster. Check namespace/resource ownership, distinct ingress names, quota/RBAC,
student credential scope, and Showroom isolation. Audit cluster-scoped setup
separately. Tenant A's users and cleanup must not affect tenant B or global
cluster configuration. Retain required unique tenant identifiers even when old
GUID formatting is removed.

Remove only verified tenant-owned resources and release its allocation according
to the original ownership contract. Reconcile partial failures. Do not stop,
capture, restore, destroy, or release the shared cluster as tenant cleanup.
If shared boundaries cannot be established, block shared migration and explain
the dependency; do not silently convert it into a dedicated offering.

## Prepare catalog promotion

Generate reviewable test, prod, and optional event stages using repository
conventions. Dedicated stages use the same tested pattern ID and content
evidence; select `pattern_workloads` only for explicit post-restore customization,
otherwise `pattern`. Pin workload/Showroom refs when they affect release behavior;
mutable content overrides can change a supposedly tested deployment.

Tenant stages retain allocation settings and pin tested workload/content refs;
no cluster pattern is created. Hybrid releases record both kinds of artifact.
List stage-specific settings and schema/merge results. Do not publish or change
catalog refs until authorized, and do not claim a production runtime check merely
because candidate YAML passed static validation.
