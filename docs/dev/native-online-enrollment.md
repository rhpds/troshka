# Enroll existing native projects without stopping VMs

## Reviewed custody successors

For a project that already has custody, use the explicit-target operator CLI's
`prepare-reconciliation` command with repeated `--project-id`, a `--reason`, and
an `--output` path. Select each accounting-only project to promote with
`--cleanup-authority-project-id`. Preparation pins the current predecessor and
does not grant cleanup authority.

Pass that output through the existing `verify-online` command using independently
selected pins, backend configuration, collector kubeconfig, and administrator
attestation. Run `review-reconciliation --enrollment <verified-file> --output
<review-file>` and inspect the before/after evidence. Apply only the reviewed
document with `apply-reconciliation --enrollment <verified-file> --review-digest
<review_digest>` and the same explicit cleanup-authority selection. Every command
still requires the database URL and reporting target UUID.

Application requires verification within 60 seconds. If verification must be
repeated, review its new digest again. Existing receipts remain immutable;
successors are append-only. Changed predecessors, active operations, unavailable
job tracking, and existing cleanup anchors reject the transition. Recovery never
silently retargets an issued cleanup request. See the
[release gate](project-deletion-release-gate.md) for remaining production blockers.

### Replaced disks with independently proven removal

An added disk does not settle an original disk. Reconciliation rejects a missing
original binding unless an administrator explicitly selects a protected retirement
receipt for that exact binding. Apply migrations through `d90bc31e7426` before
using this workflow. The original custody and retirement evidence remain stored.

For each removed binding, use these explicit-target commands from
`scripts/import_kubevirt_storage.py`:

1. `prepare-binding-retirement --project-id <id> --pvc-uid <original-uid>
   --administrator <identity> --reason <reason> --output <request.json>`.
   If the root generation changed, supply `--root <protected-root.json>` containing
   the exact current root; root identity cannot change.
2. `verify-binding-retirement --request <request.json> --pins <pins.json>
   --inventory <inventory.json> --backend-config <backend.json> --kubeconfig
   <collector-kubeconfig> --output <evidence.json>`. These are the protected managed
   collector profile inputs. Verification performs provider reads only.
3. `review-binding-retirement --evidence <evidence.json> --output <review.json>`.
   Inspect the original custody identity, binding, private ancestry, and complete
   absence evidence. Verification must establish absence of original PVC/PV/CSI
   references, private images in both active storage and trash, and obsolete clone
   source snapshots. Unknown or incomplete inventory blocks the operation.
4. `apply-binding-retirement --evidence <evidence.json> --review-digest <digest>
   --output <references.json>`. This records reviewed evidence; it grants no
   deletion authority and releases no storage liability by itself.

Every command requires `--target-id` and an explicit database URL, as above.
Retirement apply requires evidence verified within 60 seconds. Reverify and review
again if it expires; each retirement verification scan must also finish within
60 seconds.
The reference output is a JSON list of `{ "receipt_id": "...", "digest": "..." }`.
For multiple original bindings, combine only the reviewed references into one
protected, mode-0600 JSON file.

Pass that file as `--binding-retirements <references.json>` to both
`prepare-reconciliation` and `apply-reconciliation`. Final online verification
rechecks absence for those exact immutable references and includes the observations
in the reviewed custody digest. Its remaining root and CSI checks can take longer
than the retirement observation's admission window. Successor apply requires the
final online verification to be within 60 seconds and every retirement observation
to fall between its protected preparation and that final verification. It does not
relabel earlier observations or treat metadata reconciliation as deletion evidence.

Applying a successor with retirement references invalidates the domain's observation
cursors, including sibling project scopes, while retaining report history and
conflict flags. A cutoff watermark rejects in-flight reports that began before or
at that invalidation. Cleanup must obtain a new scan that independently confirms
all relevant original resources and source snapshots remain absent. A returning
resource or incomplete scan blocks cleanup. Shared cache source images are never
selected for retirement or deletion by this workflow. These separate checks do not
extend the retirement-admission or final-online-verification freshness windows.

## Initial enrollment

Online custody enrollment records administrator-approved ownership of existing
native infrastructure. It uses provider reads and database writes only. It does
not stop or restart workloads, enable quotas, change project owners, reconstruct
historical costs, or create deployment reservations.

Use this for stable active or stopped native projects that have retained deployed
topology but no existing quota ledger or allocations. Projects already managed
by the ledger use ordinary reconciliation. Keep quota enforcement disabled.

Cleanup-only compatibility validates legacy console access roles, service
accounts, S3 credential Secrets, BMC services, cluster grouping, Ceph appliances,
and VM-owned persistent-state PVCs against their expected Troshka configuration.
The receipt pins stable object identities and configuration; it excludes Secret
values. This does not relax ordinary quota admission requirements.

For existing CDI COW clones, verification also records present-day ancestry
custody. It checks the current CSI journal and bound head image, the private
intermediate's immutable identity and exclusive child relationship, and the
independently bound shared cache source and snapshot links. This evidence is
separate from historical managed-deploy clone receipts. Missing, changed, or
unaccounted relationships block enrollment or cleanup; names alone do not grant
storage ownership.

Recognized legacy ops and workload-runner pods remain running during enrollment.
On a later Delete/Undeploy, cleanup must prevent new launches, stop the exact
pinned writers and confirm their absence before treating the VM as safely off.
Neither custody nor a successful deletion request is proof that storage is gone.

Verification reads the ancestry twice, then rechecks namespace object identities
and storage bindings before issuing the receipt. Unknown source children or snapshots fail verification; include
all affected project roots in the reviewed domain rather than claiming foreign
edges. Shared source images remain outside project cleanup ownership.

The later collector charges both private head and intermediate bytes and requires
confirmed absence of both image IDs from active storage and trash, plus removal
of their obsolete source snapshot, before settlement. Existing CSI cleanup owns
that progression; enrollment introduces no backend deletion or shared trash purge.

Dev live acceptance passed on 2026-11-14. Enrollment preserved the VMI identity,
creation time, guest boot ID and disk checksum without stopping the VM. Subsequent
ordinary Stop/Start through Troshka preserved the disk checksum. Troshka Undeploy
finished with both private image IDs absent from active storage and trash, the
shared source intact, and the project owner and canvas preserved. Unrelated
projects, images, patterns and library records retained their inventory hashes.

All four existing production namespaces also pass the read-only compatibility
profile. This is not production enrollment: production still requires fresh
domain-specific ancestry verification and enrollment after the tested release
is packaged. Dev currently uses temporary source overlays; production was not
modified during this acceptance run.

## Preparation

Apply the `ab841d2e6109` migration through the reviewed deployment process. Set
`TROSHKA_NATIVE_REPORT_DATABASE_URL` securely for the intended database. Use the
target UUID, protected backend configuration, static kubeconfig and independently
verified pins described in [native storage reporting](native-storage-reporting.md).
The kubeconfig must identify the same cluster as the database-configured provider.
Online verification currently accepts one provider/backend domain per invocation.

Prepare a request for the existing projects:

```sh
umask 077
src/backend/venv/bin/python3 scripts/import_kubevirt_storage.py \
  --target-id <target-uuid> prepare-online \
  --project-id <project-uuid> --output online-request.json
```

Repeat `--project-id` when the domain contains multiple projects. The domain pins
must cover the requested roots and comply with existing pool ownership rules;
do not replace an existing domain with a partial inventory.

Create a private `custody-attestation.json` file containing the actual reviewing
administrator and reason:

```json
{
  "administrator": "<administrator identity>",
  "reason": "Reviewed existing Troshka project namespace and retained disks",
  "namespace_custody": true
}
```

This grants custody of the project namespace for eventual owner-requested
cleanup. Review the namespace contents before attesting. The local CLI is a
privileged database-administrator interface, not a user-facing ingestion API.

### Reviewed legacy topology differences

Normal enrollment requires the saved deployment and live topology to match after
the documented deployment-finalization projection. Older projects can contain
post-deployment installer, route, or editor updates in only one copy. Do not
rewrite either copy or broadly ignore fields to pass this check.

After reviewing the differences for a specific project, an administrator may add
`topology_reviews` to the attestation. It maps each reviewed project UUID to
`saved_digest` and `live_digest`, computed with
`app.services.kubevirt_storage_enrollment.digest` from the complete saved
`deployed_topology` and complete live TroshkaProject `spec.topology`, respectively.
Do not hash the canonical projection or automatically approve newly observed
values. Capture the hashes during review; any subsequent change requires review
again. Never export credential-bearing topology contents into logs.

Only projects in the prepared request may have a review. The receipt retains
both exact versions, and both hashes are checked during verification and receipt
validation. The saved deployment must still match at application and enrollment
reconciliation. Later cleanup uses the receipt's original root and storage
identities, so canvas edits and installer metadata updates do not invalidate
cleanup authority. It does not use the edited database topology to select
resources for deletion. Namespace identity, provider identity, object custody, PVC/PV bindings,
and COW ancestry checks remain mandatory. This exception grants cleanup-only
compatibility, not valid compute accounting or permission to enable quotas.

Omit `topology_reviews` for ordinary enrollment. Empty reviews are omitted from
serialized evidence so existing receipt digests remain valid.

Retained ancestry resolution also validates receipt integrity and current
owner/provider identity independently of canvas metadata. Current collection
and graph validation still require the exact original storage bindings and
image relationships; this does not refresh stale observations.

Cleanup comparison permits a Deployment's added
`kubectl.kubernetes.io/restartedAt` pod-template annotation only when removing
that annotation reproduces the original attested digest. Images, mounts,
network annotations, UIDs and owner references remain checked. Existing receipts
are never rewritten. A legacy receipt that already pinned a different restart
timestamp cannot reconstruct that timestamp from its digest and still requires
explicit reconciliation. New custody pins exclude this restart-only timestamp
so repeated routine releases do not invalidate them. Sealed deletion identities
remain exact.

## Verify and enroll

```sh
src/backend/venv/bin/python3 scripts/import_kubevirt_storage.py \
  --target-id <target-uuid> verify-online \
  --request online-request.json --pins enrollment-pins.json \
  --backend-config backend-identities.json \
  --kubeconfig /absolute/path/to/protected-kubeconfig \
  --attestation custody-attestation.json --output online-verified.json

src/backend/venv/bin/python3 scripts/import_kubevirt_storage.py \
  --target-id <target-uuid> enroll-online --enrollment online-verified.json
```

Apply within 60 seconds of verification completion. Final identity checks and
the receipt timestamp follow the bounded ancestry scan. If evidence expires, verify again;
identity or topology changes require fresh preparation. A conflicting existing
custody receipt requires explicit reconciliation rather than silent replacement.

### Adding projects that share enrolled base images

With quotas disabled, ordinary deployments do not create quota reservations or
managed-clone receipts. A new project can therefore add legitimate children to
an enrolled cache image before accounting knows their ownership. The shared
storage check correctly remains unavailable until these children are verified;
this is not evidence that the clones should be deleted.

Prepare and verify the complete domain, including previously enrolled projects
and the new siblings. Preserve all existing roots and collection identities in
the pins, and use a `domain_extension` referencing the current enrollment ID and
digest when adding roots. Review namespace custody and any exact topology
differences as above. Apply the fresh combined verification with:

```sh
src/backend/venv/bin/python3 scripts/import_kubevirt_storage.py \
  --target-id <target-uuid> enroll-online --enrollment online-verified.json \
  --preserve-existing
```

This explicit reconciliation retains existing receipts byte-for-byte only when
their current owner/provider identity, saved and live topology, object identities,
volume bindings and clone ancestry still match. Verification timestamps and
sibling project membership may differ. New projects receive their own receipts.
Changed existing evidence still blocks the transaction; the flag does not replace
receipts, enable quotas, change workloads or waive exact clone graph validation.
Repeat enrollment for later quota-disabled deployments that introduce new roots;
the managed collector automatically extends only admitted quota operations.

Capture VM instance UIDs and creation times before and after enrollment, and
verify application availability and unchanged project ownership. A successful
database enrollment alone is not evidence of uninterrupted guest operation.

## Later cleanup

### Accounting-only evidence for failed deployments

An administrator may explicitly select a failed project during preparation with
`prepare-online --project-id <id> --accounting-only-project-id <id>`. Repeat both
options as necessary; the accounting-only IDs must be part of the prepared set.
This permits terminal `error` state while still rejecting draft and transitional
states. Retained deployed topology, exact namespace ownership, storage identities,
clone ancestry, fresh verification, disabled quotas, and absence of quota ledger
records remain required. Review topology differences independently before attesting.

The selected IDs are persisted in the prepared request and protected by its digest.
New receipts for these projects cannot authorize cleanup, even after a later state
change. Existing matching receipts retain their original contents and authority.
Enrollment leaves the project state, failure details, workloads, and disks intact;
it neither resumes deployment nor proves complete deployment or zero compute usage.
To grant cleanup authority later requires a separate explicit reconciliation, not
removal of this restriction from a saved receipt.

For a previously attested project whose network support was partially removed,
the Python preparation API additionally accepts
`accounting_only_missing_object_uids={project_id: [exact_missing_uid, ...]}`.
The project must also be explicitly selected in `accounting_only_project_ids`.
Select the exact missing identities after an independent live inventory comparison;
the preparer permits only retained network CRs, network attachment definitions,
DNS support ConfigMaps/deployments, and gateway deployments. It records the old
receipt ID, digest, full stable identities, and selected missing UIDs in the
prepared request. There is no general permission to ignore missing support.

Verification requires every remaining stable object to match that receipt exactly,
no new stable objects, and exactly the selected omissions. Dynamic ownership,
volume/mount restrictions, active-work checks, and fresh root/PVC/PV/CSI/clone
verification still apply. An unchanged retained helper may be accounted for even
when its selected network parent is absent. Failed partial deployments may lack
VMs, but existing VMs and runtime objects remain validated; no successful runtime
state is synthesized. Applying this reconciliation requires `preserve_existing=True`
and the same unchanged original receipt. The receipt stays immutable, current
observations contain only existing objects, and ordinary cleanup preflight still
rejects the incomplete namespace. This mechanism neither repairs networks nor
grants additional power or deletion authority.

Ordinary quota-disabled Start/Stop retains its existing behavior. Delete and
Undeploy revalidate custody and disk identities, obtain a real managed shutdown
proof, and settle only the project's private disk and trash identities. The
cleanup-only ledger anchor is created at cleanup time and explicitly marks
compute accounting unavailable. It must never be interpreted as measured zero
usage or permission to enable quotas.

While cleanup-only custody makes complete quota usage unavailable, the usage API
returns HTTP 503 with reason `accounting_unavailable`, after authorization. It
does not expose partial custody values as zero usage. Collection may temporarily
report unavailable while deletion changes the storage graph; settlement requires
a subsequent accepted report with the exact private image absence evidence.

Enrollment is not a reason to delete or recreate projects, PVCs, images, or
patterns. If validation fails, keep the workloads running and resolve the exact
identity or ownership conflict before promoting production.
