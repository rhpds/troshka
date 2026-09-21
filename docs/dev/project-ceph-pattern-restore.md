# Project Ceph Pattern Restore — Rook PVC-Adopt Spike Findings

> Task 0 spike for identity-preserving project Ceph pattern capture
> (`docs/superpowers/specs/2026-09-21-project-ceph-pattern-capture-design.md`).
> Answers: can Rook be recreated on top of pre-filled mon+OSD PVCs without
> wiping data/identity, and what exactly needs to be preserved for that to work.

## Result: Strategy A confirmed — Rook adopts existing mon + OSD PVCs

Deleting the `CephCluster` CR and recreating it with matching mon/device-set
config reattaches the **same** mon and OSD PVCs, and the cluster comes back
with the **same fsid**, the **same OSD identity**, and all pre-existing pool
data (RBD image + a plain `rados` object) intact. No `rbd import`, no
consumer credential rewrite.

This was verified live on `ocpvdev01` in a disposable namespace
(`troshka-spike-rookadopt`, deleted after the spike). `troshka-b57b2bab`
(the live CCLM lab project) was only read from for naming/label discovery —
never modified.

## PVC naming convention (discovered, not configurable)

Rook creates these from Troshka's `build_ceph_cluster()` templates
(`src/operator/helpers/rook_ceph.py`). Names are **not** the
`troshka-ceph-osd-{i}` convention from the unused `build_osd_pvcs()` helper —
that helper is dead code; production Ceph clusters use Rook's own
`storageClassDeviceSets` PVC-templating path instead.

| Resource | Name pattern | Example (live `troshka-b57b2bab`) | Example (spike) |
|---|---|---|---|
| Mon PVC | `rook-ceph-mon-<id>` — fixed, one per mon letter (`a`, `b`, ...) | `rook-ceph-mon-a` | `rook-ceph-mon-a` |
| OSD PVC | `<deviceSetName>-<templateName>-<random6>` where `deviceSetName="osd-set"`, `templateName="data"` (both hardcoded in `build_ceph_cluster`) | `osd-set-data-07ln8j`, `osd-set-data-184c79`, `osd-set-data-24m8n7` | `osd-set-data-06p6rg` |

OSD PVC names include a **Kubernetes-generated random suffix** (`generateName:
osd-set-data-0`), so they are never literally reproducible across a
create/delete/recreate cycle by name alone. Rook instead **discovers**
existing OSD PVCs by label before creating new ones:

```yaml
labels:
  ceph.rook.io/DeviceSet: osd-set          # matches spec.storage.storageClassDeviceSets[].name
  ceph.rook.io/DeviceSetPVCId: osd-set-data-0   # <deviceSetName>-<templateName>-<index>
  ceph.rook.io/setIndex: "0"
```

The mon PVC is matched by its **fixed name** (`rook-ceph-mon-a`), not a label.

**Restore-mode implication:** discovery code for Phase 1+ should match OSD
PVCs by the `ceph.rook.io/DeviceSet` + `ceph.rook.io/DeviceSetPVCId` labels
(recorded per `PatternDisk.source_index` at capture time), not by
reconstructing the random-suffixed name. The mon PVC can be recreated with
the exact fixed name `rook-ceph-mon-<id>`.

## The real blocker: ownerReferences, not the PVCs alone

Every Rook-managed object for a `CephCluster` — **not just the mon/OSD
PVCs** — carries `ownerReferences: [{kind: CephCluster, controller: true,
blockOwnerDeletion: true}]`. **A plain `oc delete cephcluster` (or `kopf`'s
existing `ceph_delete` handler, which additionally force-deletes PVCs)
cascades through Kubernetes garbage collection and deletes all of it**,
including the `rook-ceph-mon` secret holding the fsid.

The spike's Step 2 experiment orphaned (stripped `ownerReferences` from) the
**entire** set below **together, all at once**, before deleting
`CephCluster` — it did **not** test any object individually, so "survives
when orphaned" is confirmed for the whole set as a group, not per-object.
The MUST-capture / may-regenerate column below is a mix of that group
evidence and reasoning from what each object actually stores; low-confidence
rows are called out explicitly rather than asserted as tested.

### Identity-object capture matrix

| Object | Kind | Orphaned+preserved together in spike? | What it carries | Capture requirement |
|---|---|---|---|---|
| `rook-ceph-mon-a` (mon PVC) | PVC | ✅ yes | Mon rocksdb store (monmap, osdmap, auth db) | **MUST capture** — this is the `PatternDisk` itself |
| `osd-set-data-*` (OSD PVCs) | PVC | ✅ yes | OSD bluestore data | **MUST capture** — this is the `PatternDisk` itself |
| `rook-ceph-mon` | Secret | ✅ yes | `fsid`, `mon-secret`, `admin-secret` | **MUST capture** — this *is* the cluster identity; confirmed fsid survived only because this secret survived |
| `rook-ceph-mon-endpoints` | ConfigMap | ✅ yes | mon addr mapping, `maxMonId` | **MUST capture** — op-mon reads this before deciding whether a mon is "new" |
| `rook-ceph-admin-keyring` | Secret | ✅ yes | `client.admin` cephx key | **MUST capture** — used by export job / any admin-level access; matches the key already recorded in the mon's persisted auth db, so a regenerated one would mismatch |
| `rook-ceph-mons-keyring` | Secret | ✅ yes | mon cephx key | **MUST capture** — same reasoning as admin keyring |
| `rook-ceph-config` | Secret | ✅ yes | rendered `ceph.conf` | **MUST capture** — cheap, avoids Rook re-deriving it from a possibly-incomplete CR state mid-restore |
| `rook-csi-rbd-node` / `rook-csi-rbd-provisioner` | Secret | ✅ yes | `client.csi-rbd-*` cephx keys | **MUST capture** — baked into nested consumers' CSI config; a fresh key would not match the entity already registered in the mon auth db, breaking nested mounts |
| `rook-csi-cephfs-node` / `rook-csi-cephfs-provisioner` | Secret | ✅ yes | `client.csi-cephfs-*` cephx keys | **Capture for completeness**, but Troshka's project Ceph does not expose CephFS today — low risk if skipped, **not independently verified** |
| `rook-ceph-mgr-a-keyring` | Secret | ✅ yes | `client.mgr.a` cephx key | **Should capture** — same mon-auth-db-mismatch risk as admin/mon keyrings if regenerated fresh, but only the mgr daemon itself would be affected (not external consumers); **not independently tested in isolation** |
| `rook-ceph-crash-collector-keyring` | Secret | ✅ yes | `client.crash` cephx key | **Low priority** — crash-collector is disabled in Troshka's `CephCluster` spec (`crashCollector.disable: true`); safe to omit, **not independently tested** |
| `rook-ceph-exporter-keyring` | Secret | ✅ yes | `client.ceph-exporter` cephx key | **Low priority** — metrics-only sidecar; a regenerated key would at worst break metrics scraping, not data/identity; **not independently tested** |
| `cluster-peer-token-troshka-ceph` | Secret | ✅ yes | RBD-mirroring peer bootstrap token | **Skip / let Rook regenerate** — Troshka does not use RBD mirroring between clusters; this token has no consumer outside Rook's own (unused) mirroring feature |

**Practical recommendation:** capture the full "MUST capture" + "Should
capture" rows (everything except the bottom three low-priority rows) as
part of `projectCephCapture` metadata — it's a handful of small Secrets plus
one ConfigMap, cheap to snapshot as plain string data (no VolumeSnapshot
needed), and capturing the whole group matches exactly what the spike
proved works. Treating any row as "safe to skip" beyond the bottom three
is an *optimization* for a later task, not something this spike validated —
if Phase 1 wants to trim the list further, it must re-run the orphan
experiment with that specific object *excluded* to confirm Rook truly
regenerates it cleanly against the existing mon auth db.

For pattern restore this maps to: capture these Secrets/ConfigMap alongside
the mon/OSD `PatternDisk`s (small — no VolumeSnapshot needed, just their
`data`), and pre-create them (without an owning `CephCluster`, since it
doesn't exist yet) before creating the restore-mode `TroshkaCeph`/`CephCluster`.

## Do not reuse standard teardown for capture or restore-preservation paths

`kopf`'s existing `ceph_delete` handler
(`src/operator/handlers/ceph.py`) is **intentionally destructive**: on
`TroshkaCeph` CR delete it deletes the `CephCluster`/`CephBlockPool` CRs and
then explicitly calls `delete_ceph_storage_pvcs()`
(`src/operator/helpers/rook_ceph.py`), which force-deletes mon/OSD PVCs by
label and name-prefix regardless of ownership state. That is correct and
wanted for a real project teardown — it is **not** the mechanism used
anywhere in this spike, and must never be invoked on artifacts that a
capture or restore flow needs to survive:

- **Capture** never touches the source project's `CephCluster`/`TroshkaCeph`
  CR at all — it only quiesces, snapshots PVCs, and unfreezes. There is no
  code path in the capture design that should call `ceph_delete` or
  `delete_ceph_storage_pvcs()`; if any future capture code does, that's a bug.
- **Restore** creates a brand-new `TroshkaCeph`/`CephCluster` in a project
  that has no prior Ceph — again, `ceph_delete` is simply never in the
  restore path under normal operation.
- The **only** place this spike deliberately produced a delete+recreate
  cycle was the Step 2 experiment itself, and it explicitly did **not** use
  `ceph_delete` / `delete_ceph_storage_pvcs()` — it used the
  orphan-ownerReferences-then-`oc delete cephcluster` pattern documented
  above and in "Spike procedure" step 5–7. If a future task needs an
  in-place "reset and reattach" repair flow (e.g. recovering from a failed
  restore, or re-running restore against already-materialized PVCs), it
  **must** follow that same orphan-first pattern — strip `ownerReferences`
  from every object in the capture matrix above before deleting the
  `CephCluster`/`CephBlockPool` CRs, and must not call the standard
  `ceph_delete` handler or `delete_ceph_storage_pvcs()` against artifacts
  that need to survive. Calling the standard teardown on a restore-in-progress
  project will permanently delete the mon/OSD PVCs and their data.

## Spike procedure (what was actually run)

1. Tiny `TroshkaCeph` (`capacityGi: 150`, `osdCount: 1`) in disposable ns
   `troshka-spike-rookadopt` on `ocpvdev01`.
2. **Lab-specific scheduling wrinkle** (not a Rook/Troshka design issue, but
   blocked the spike until worked around): every real worker/infra node in
   this cluster except `ocp-virtdev1-host4` carries
   `cluster.ocs.openshift.io/openshift-storage=` (ODF-dedicated), which is
   exactly the label Troshka's mon/OSD `nodeAffinity` requires to be absent.
   Combined with `network.provider: host` (hostNetwork mon on 6789/3300),
   only **one** TroshkaCeph mon can run cluster-wide at a time — and
   `troshka-b57b2bab`'s mon already occupied that slot. Worked around for
   the spike only by patching the spike's `CephCluster` to
   `network.provider: ""` (pod network instead of host) and clearing
   `placement.*.nodeAffinity`, which let the mon/OSD land on `host4`
   alongside the existing mon without a port conflict. This is a real
   capacity constraint of the shared dev cluster, unrelated to the
   PVC-adopt question — worth flagging to whoever owns lab capacity before
   Phase 4 integration testing tries to run two project-Ceph clusters
   concurrently for real.
3. Cluster reached `Ready` / `HEALTH_OK`; recorded fsid
   `ef18a3d0-dc89-4438-840b-ff9d65401b4b`.
4. Wrote known data: `rbd create troshka-ceph-pool/spike-test-image` (64Mi,
   id `10da4ec2cf75`) + `rados put troshka-ceph-pool/spike-marker-object`
   with a unique marker string. (Connecting a manual `ceph`/`rbd` client
   requires the mon's **ClusterIP service address**, e.g. from
   `ROOK_CEPH_MON_HOST` in the mon pod's env — using the mon pod's raw IP
   fails auth negotiation because Rook's monmap advertises the service
   address, not the pod IP.)
5. Orphaned (stripped `ownerReferences` from) both PVCs and the full
   identity-secret set above.
6. `oc delete cephcluster troshka-ceph` — required also deleting the
   dependent `CephBlockPool` CR (Rook's own finalizer blocks `CephCluster`
   deletion until dependent CRs are gone; the pool CR itself refused
   deletion because it "contains images" — resolved by stripping the
   `CephBlockPool`'s finalizer directly, which removes the k8s object
   without invoking Rook's `ceph osd pool rm`, i.e. the **CR** goes away but
   the **data stays**). Confirmed all mon/mgr/osd pods terminated, `fsid`
   secret and both PVCs remained.
7. Re-applied a `CephCluster` (same `mon.volumeClaimTemplate`,
   `storage.storageClassDeviceSets[0].name="osd-set"` /
   `volumeClaimTemplates[0].metadata.name="data"`) and a fresh
   `CephBlockPool` CR.
8. Cluster reconciled straight to `Ready`, reusing the **same** PVC objects
   (no new `osd-set-data-*` PVC created), and:
   - `status.fsid` == `ef18a3d0-dc89-4438-840b-ff9d65401b4b` (unchanged)
   - `rados get spike-marker-object` returned the original marker string byte-for-byte
   - `rbd info spike-test-image` showed identical `id: 10da4ec2cf75`,
     `block_name_prefix`, and original `create_timestamp`
   - Ceph reported the OSD as "1 in (since 13m)" — i.e. original CRUSH
     membership timestamp preserved, not re-added as a new OSD
9. Note: after adoption, the reused PVCs/Secrets were **not** re-parented
   with an `ownerReference` to the new `CephCluster` UID. Troshka's existing
   teardown helper (`delete_ceph_storage_pvcs()` in
   `src/operator/helpers/rook_ceph.py`) already selects PVCs by label
   (`app=rook-ceph-osd`, `app=troshka-ceph`) and name prefix, not by
   ownerReference, so cleanup still works after a restore. If any future
   code path relies on ownerReference-based GC for Ceph PVCs, it will need
   to explicitly re-stamp it after restore.
10. Cleanup: deleted `troshka-spike-rookadopt` namespace entirely.
    `troshka-b57b2bab` was never modified — confirmed still `Ready` /
    `HEALTH_OK` / same fsid / same PVCs after the spike.

## CephCluster shape for restore

Same shape `build_ceph_cluster()` already produces today
(`src/operator/helpers/rook_ceph.py`) — **no changes needed to the CR shape
itself** for Strategy A to work. What restore mode must change is
*preconditions*, not the CR:

```yaml
spec:
  mon:
    count: 1
    volumeClaimTemplate:
      spec:
        storageClassName: <same as capture>
        accessModes: [ReadWriteOnce]
        resources: {requests: {storage: <mon size>}}
  storage:
    useAllNodes: false
    useAllDevices: false
    storageClassDeviceSets:
      - name: osd-set              # must match captured ceph.rook.io/DeviceSet label
        count: <osdCount>
        portable: true
        volumeClaimTemplates:
          - metadata: {name: data}  # must match captured ceph.rook.io/DeviceSetPVCId prefix
            spec:
              volumeMode: Block
              storageClassName: <same as capture>
              resources: {requests: {storage: <per-osd size>}}
```

Restore-mode preconditions (before creating this `CephCluster`):
1. Pre-create mon PVC named `rook-ceph-mon-a` (bound from the restored mon `PatternDisk`).
2. Pre-create OSD PVC(s) with `generateName`-equivalent fixed names is not
   required — Rook matches by label, so pre-created OSD PVCs just need the
   `ceph.rook.io/DeviceSet` / `ceph.rook.io/DeviceSetPVCId` / `ceph.rook.io/setIndex`
   labels set correctly per captured `source_index`.
3. Pre-create the identity Secrets/ConfigMap from captured values (fsid,
   mon-secret, admin-secret, csi user keys, ...) — see the **identity-object
   capture matrix** above for the full "MUST capture" / "Should capture"
   list — **without** an owning `CephCluster` (none exists yet).
4. Pre-create (or let Rook create fresh) the `CephBlockPool` CR — see
   "CephBlockPool restore semantics" below; this is a declarative wrapper,
   not a data-carrying object, so it has looser requirements than steps 1–3.
5. Only then create the `TroshkaCeph`/`CephCluster` CR.

## CephBlockPool restore semantics

Unlike the mon/OSD PVCs and identity Secrets, the `CephBlockPool` CR
(`troshka-ceph-pool`) is **not** where pool data or identity lives — it is a
declarative instruction telling Rook "ensure a pool with this name/shape
exists." The pool's actual data (RBD images, objects, PG layout) lives
inside the already-adopted OSDs, keyed by the fsid/OSDMap that came back
with them.

Confirmed in the spike:

- Deleting the `CephBlockPool` **CR** does **not** delete the underlying
  Ceph pool or its data, *if* the CR is removed by stripping its finalizer
  instead of letting Rook's own reconcile run the delete to completion.
  Rook's finalizer logic actively **refuses** to run `ceph osd pool rm`
  while the pool "contains images or snapshots" — the spike hit this refusal
  directly (`pool "troshka-ceph-pool" cannot be deleted because it is not
  empty or has dependents`) and worked around it by force-clearing the CR's
  finalizer (`oc patch cephblockpool ... --type=json -p '[{"op":"remove",
  "path":"/metadata/finalizers"}]'`), which removes the Kubernetes object
  without ever invoking Rook's pool-delete logic.
- A **freshly-created** `CephBlockPool` CR (new UID, no relationship to the
  old one) reconciled cleanly to `Ready` against the already-existing pool.
  The spike's marker `rados` object and `rbd` image — written before the
  delete/recreate cycle — were still present afterward, confirming Rook
  detected the pool already existed at the Ceph level and did not recreate
  it from scratch.

**Metadata expectations for the recreated `CephBlockPool` CR:**

- `metadata.name` — fixed by Troshka's convention
  (`CEPH_BLOCK_POOL_NAME = "troshka-ceph-pool"` in
  `src/operator/helpers/rook_ceph.py`). No capture needed; restore mode
  reuses the same constant.
- `spec.failureDomain` — fixed (`"host"`) by the same constant builder. No
  capture needed.
- `spec.replicated.size` — **must match** the pool's actual replication
  size at capture time, not just default to `TroshkaCeph.spec.replicateSize`
  blindly. This value is already part of existing topology capture
  (`cephClusterNode` → `replicateSize`, per the design doc's Topology
  section), so no *new* capture work is needed — restore mode just has to
  thread that already-captured value into the recreated `CephBlockPool`
  CR's `spec.replicated.size` rather than recomputing it from a possibly
  different current `osdCount`. **Why it matters:** if the recreated CR's
  `replicated.size` does not match what the pool already has, Rook will
  issue a live `ceph osd pool set ... size` reconcile against the just-
  recovered pool — not destructive, but an unwanted operational side effect
  (and a real risk of an under/oversized-PG health warning) that a correct
  restore should avoid by matching the captured value exactly.
- No other pool-level settings (`replicated.size` aside) were exercised by
  this spike — `troshka-ceph-pool` only ever uses `failureDomain: host` +
  `replicated.size`, matching `build_ceph_block_pool()`.

## Recommendation

**Proceed with Strategy A.** No pivot to Strategy B (explicit
`troshka-ceph-osd-*` PVCs via `build_osd_pvcs`) or Strategy C (abort) needed.
Downstream tasks should:
- Capture the OSD/mon `PatternDisk`s exactly as designed (§5 of the design doc).
- Additionally capture the identity Secrets/ConfigMap in the **identity-object
  capture matrix** above (not full VolumeSnapshots — just their string
  data) as part of the `projectCephCapture` metadata, since fsid/keys live
  there, not just in the raw block devices.
- Thread the already-captured `replicateSize` topology value into the
  restore-mode `CephBlockPool` CR's `spec.replicated.size` (see
  "CephBlockPool restore semantics") to avoid an unwanted live pool resize.
- Recreate PVCs + those Secrets/ConfigMap (unowned) before creating the
  restore-mode `TroshkaCeph`, matching the label scheme above for OSDs —
  and never via the standard `ceph_delete` / `delete_ceph_storage_pvcs()`
  teardown path (see "Do not reuse standard teardown" above).
- `build_osd_pvcs()` in `rook_ceph.py` remains dead code; do not wire it in
  for restore — Rook's own device-set PVC discovery already does the job.

## `spec.restore` (Task 5 implementation)

`TroshkaCeph.spec.restore` is a real CRD field (`troshkanceph.yaml`), not an
annotation fallback:

```yaml
spec:
  restore:
    enabled: true
    monPvc: rook-ceph-mon-a         # must equal Rook's fixed mon PVC name
    osdPvcs: [osd-set-data-06p6rg, osd-set-data-184c79]
```

As established above, **Rook has no CephCluster field that means "attach PVC
X"** — this block cannot make Rook literally reference those PVC names. What
it carries:

- `monPvc` / `osdPvcs` — traceability metadata (which PVCs this restore
  expects Rook to adopt); validated (`monPvc` must equal the fixed
  `rook-ceph-mon-a` name — Rook can never adopt a differently-named mon PVC)
  and stamped onto the `CephCluster`'s `metadata.annotations` for audit, not
  consumed by Rook itself.
- `len(osdPvcs)` — the one value that changes manifest *shape*:
  `build_ceph_cluster(..., restore=...)` sets
  `storage.storageClassDeviceSets[0].count` to `len(osdPvcs)` instead of the
  spec-computed `osdCount`, so Rook's reconcile never mints new empty OSD
  claims to top up to a configured count that doesn't match how many PVCs
  were actually pre-created by the restore-materialize step (Task 9). If
  `osdPvcs` is empty (malformed restore block), it falls back to the
  spec-computed count rather than requesting zero OSDs.

`normalize_restore_spec(spec)` and `handlers/ceph.py`'s `_reconcile_ceph` read
this field; when `restore.enabled`, the handler logs the adopted PVC names/
count and sets `status.restoreMode: true` before doing the ordinary Rook
operator + `CephCluster`/`CephBlockPool` apply (unchanged reconcile path
otherwise — pre-creating the actual PVCs + identity secrets is Task 9/10, not
this builder).

## Boot gate (Task 10 implementation)

Restore-mode PVCs are adopted (Task 9) but Rook still needs real reconcile
time to bring OSDs/mon up before RBD is actually usable. `handlers/project.py`
gates VM power-on on the restored `TroshkaCeph` reaching Ready:

- `_create_ceph_cr` stamps `patch.status["cephRestoreActive"] = True` on the
  `TroshkaProject` the moment `_materialize_ceph_restore` reports a non-empty
  `ceph_spec["restore"]` — i.e. only for actual restore-mode deploys. Fresh
  bootstrap (no `projectCephCapture`) and legacy no-Ceph projects never set
  this key, so their VM-start path is byte-for-byte unchanged.
- `_handle_vm_start` (called every 10s from the existing
  `project_status_check` timer, not a new blocking wait) checks
  `status["cephRestoreActive"]`; if set, it calls `_ceph_cr_ready()` — a
  direct read of the `TroshkaCeph` CR's own `status.phase` (set by
  `handlers/ceph.py`'s `_reconcile_ceph`, not the raw Rook `CephCluster`) —
  and defers (`deployProgress.stage = "Waiting for restored Ceph"`) until it
  reports `"Ready"`.
- The gate applies to **every** VM in the project, not just ones with Ceph-
  backed disks — the plan's "safest general-purpose rule," since topology
  doesn't cheaply distinguish Ceph-backed VMs from others at this call site
  and getting it wrong in the unsafe direction risks booting against an
  unready/empty pool.

## Dev E2E checklist (manual)

**Purpose:** End-to-end validation of identity-preserving project Ceph pattern
capture and restore on a real KubeVirt cluster. This is **not** automated in
CI — it is the operator gate before trusting the capture/restore path in
production-like labs. Run on **`ocpvdev01`** (or any KubeVirt host with project
Ceph enabled) with backend + worker + operator on the code under test.

**Spec:** `docs/superpowers/specs/2026-09-21-project-ceph-pattern-capture-design.md`
§6 (success criteria, thinness) and §8 (integration test).

### Thinness warning

Pattern Ceph disks are stored as **sparse/compressed qcow** sized to
**used/allocated bytes** on the mon and OSD block devices — **not** to
topology `capacityGi`. Expect:

| Pool usage at capture | Pattern artifact size (rough) |
|---|---|
| Empty / metadata only | Small (GiB-scale), ≪ `capacityGi` |
| Lightly used (demo RBD + a few objects) | Still ≪ `capacityGi` — this checklist |
| Pool near full | Pattern can be **very large** — close to actual allocated OSD data |

Identity preservation captures whole mon+OSD devices (not per-image `rbd export`),
so a heavily written pool produces a heavy pattern even when `capacityGi` is
small. **Warn catalog authors:** lightly used pools stay small; a full pool yields
a large pattern. UI surfacing of this trade-off is optional/future — operators
should set expectations when authoring or reviewing patterns with project Ceph.

### Pre-flight

- [ ] KubeVirt host connected (`providerType: kubevirt`); project Ceph palette
  item available.
- [ ] Backend + worker + operator restarted on the code under test
  (`./dev-services.sh restart backend` / `restart worker`; operator image or
  local reconcile on target cluster).
- [ ] Lab capacity: on shared dev clusters only one hostNetwork mon may run
  cluster-wide (see spike "Lab-specific scheduling wrinkle" above). Use a
  **disposable namespace** or confirm no mon port conflict with live projects.
- [ ] S3/RGW pattern storage reachable from the source cluster (Capture v2 OBC
  path).

### A. Source project — small Ceph, known RBD, VM client keyring

Create a minimal KubeVirt project with project Ceph and at least one VM that
will act as a Ceph client after restore.

- [ ] Deploy a project with a **`cephClusterNode`** — prefer tiny sizing for
  fast iteration: `capacityGi: 150`, `osdCount: 1` (or `3` if policy requires
  minimum replica size). Record project id / namespace `troshka-<pid8>`.
- [ ] Wait for `TroshkaCeph` / Rook `CephCluster` **`Ready`** / `HEALTH_OK`.
- [ ] Record baseline identity **before capture**:

```bash
NS=troshka-<pid8>
oc -n "$NS" get troshkaceph troshka-ceph -o jsonpath='{.status.phase}{"\n"}'
oc -n "$NS" exec deploy/rook-ceph-tools -- ceph fsid
oc -n "$NS" get pvc | rg 'rook-ceph-mon|osd-set-data'
```

- [ ] Write **known data** into `troshka-ceph-pool` (marker object + RBD image):

```bash
# From rook-ceph-tools (or mon pod with ceph/rbd CLIs)
MARKER="e2e-$(date -u +%Y%m%dT%H%M%SZ)"
echo -n "$MARKER" | rados put troshka-ceph-pool/e2e-marker-object -
rbd create troshka-ceph-pool/e2e-test-image --size 64M
rbd info troshka-ceph-pool/e2e-test-image   # record image id + create_timestamp
```

- [ ] On a **lab VM** in the same project, install a Ceph client keyring +
  `ceph.conf` using the **capture-time** topology credentials (stamped
  `labIp`, fsid, admin or dedicated client key from project topology / external
  secret — **do not hand-edit for restore**). Confirm the VM can:

```bash
ceph -s                          # reaches mon at topology labIp
rados get troshka-ceph-pool/e2e-marker-object - | cmp - <(echo -n "$MARKER")
rbd map troshka-ceph-pool/e2e-test-image && rbd unmap ...
```

- [ ] Leave the VM powered on (or note `restart_after` intent) so restore can
  prove the **same keyring** works without Troshka rewriting secrets.

### B. Capture pattern

- [ ] From the Troshka UI (or API), **Save as Pattern** on the source project.
  Wait until capture completes and pattern status is **`available`**.
- [ ] Confirm pattern topology has **`projectCephCapture`** with `monDiskId` +
  `osdDiskIds` matching captured `PatternDisk` rows (and runtime `projectCeph`
  block stripped — no live namespace refs on the pattern).
- [ ] Confirm **sparse thinness**: for each Ceph `PatternDisk`, stored object
  size ≪ topology `capacityGi` (and ≪ raw PVC `status.capacity`):

```bash
# Example: list pattern disks + S3/OBC sizes via host-db or API
./scripts/host-db.sh "
from app.models.pattern import PatternDisk
disks = session.query(PatternDisk).filter_by(pattern_id='<pattern-id>').all()
for d in disks:
    if d.source_kind in ('ceph-mon', 'ceph-osd'):
        print(d.name, d.source_kind, d.size_bytes, d.source_pvc_name)
"
```

  **Pass:** lightly used pool → Ceph disk artifacts are GiB-scale while
  `capacityGi` is 150+ (orders of magnitude smaller unless the pool was filled).
- [ ] Capture job logs show freeze → parallel mon/OSD export → unfreeze; no
  partial Ceph capture (`available` with missing OSD disk = fail).

### C. Deploy pattern to new project

- [ ] Create a **new** project from the captured pattern (new namespace, remapped
  node ids — `labIp` on `cephClusterNode` should match capture intent).
- [ ] Deploy. Confirm restore path activates:
  - `TroshkaCeph.spec.restore.enabled` (materialized from pattern)
  - `TroshkaProject.status.cephRestoreActive` set during deploy
  - VM start **deferred** with progress stage **"Waiting for restored Ceph"**
    until `TroshkaCeph` reaches **`Ready`** (boot gate, Task 10).
- [ ] Confirm mon + OSD PVCs pre-created from pattern disks (labels on OSD PVCs
  per spike — `ceph.rook.io/DeviceSet`, `DeviceSetPVCId`, `setIndex`) before
  `CephCluster` reconciles.

### D. Verify restore — identity, content, client without rewrite

- [ ] **`fsid` unchanged** vs step A baseline:

```bash
NS=troshka-<new-pid8>
oc -n "$NS" exec deploy/rook-ceph-tools -- ceph fsid
```

- [ ] **RBD pool contents present** — same marker bytes and RBD identity:

```bash
rados get troshka-ceph-pool/e2e-marker-object - | cmp - <(echo -n "$MARKER")
rbd info troshka-ceph-pool/e2e-test-image   # same id, create_timestamp as capture
```

- [ ] **VM client works without secret rewrite** — power on the restored VM
  (after Ceph Ready gate clears); **without** changing keyring, fsid, or mon
  address in cloud-init or manual edits:

```bash
# On the restored VM (same keyring files as before capture)
ceph -s
rados get troshka-ceph-pool/e2e-marker-object -
rbd map troshka-ceph-pool/e2e-test-image
```

  **Pass:** client connects using capture-time credentials; data readable. This
  is the consumer-agnostic success criterion — Troshka did not regenerate cephx
  entities that would mismatch the restored mon auth db.

### E. Source project Ceph healthy after capture

Return to the **source** project (still deployed after pattern save):

- [ ] `TroshkaCeph` / `CephCluster` back to **`Ready`** / `HEALTH_OK` (capture
  unfreezes source Ceph after exports complete).
- [ ] Source **`fsid` unchanged** from pre-capture baseline.
- [ ] Source marker object + RBD image still present (`rados get` / `rbd info`).
- [ ] Source VM Ceph client still works (optional but recommended — proves
  capture quiesce/unfreeze did not brick the live lab).

### Results (ocpvdev01)

| Step | Date | Result | Notes |
|---|---|---|---|
| Pre-flight | — | **Pending** | Checklist documented; not executed in Task 12 |
| A — Source setup | — | **Pending** | |
| B — Capture | — | **Pending** | |
| C — Deploy | — | **Pending** | |
| D — Restore verify | — | **Pending** | |
| E — Source health | — | **Pending** | |

**Sign-off:** Production trust for project Ceph pattern capture/restore requires
sections A–E above to pass on a real KubeVirt cluster. Task 0 spike (Rook PVC
adopt) is prerequisite evidence; this checklist validates the full Troshka
capture → pattern → deploy → consumer path.
