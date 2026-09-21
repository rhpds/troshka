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
blockOwnerDeletion: true}]`. Confirmed on both the live namespace and the
spike:

- `rook-ceph-mon-a` (mon PVC), `osd-set-data-*` (OSD PVCs)
- `rook-ceph-mon` Secret (**contains `fsid`, `mon-secret`, `admin-secret`**)
- `rook-ceph-admin-keyring`, `rook-ceph-mons-keyring`, `rook-ceph-config` Secrets
- `rook-csi-rbd-node`, `rook-csi-rbd-provisioner` (and cephfs equivalents) Secrets
- `rook-ceph-mon-endpoints` ConfigMap
- per-daemon keyrings (`rook-ceph-mgr-a-keyring`, exporter, crash-collector)
- `cluster-peer-token-troshka-ceph`

**A plain `oc delete cephcluster` (or `kopf`'s existing `ceph_delete` handler,
which additionally force-deletes PVCs) cascades through Kubernetes garbage
collection and deletes all of the above**, including the `rook-ceph-mon`
secret holding the fsid. If only the PVCs were preserved and the identity
secrets were lost, Rook would have no record of the previous fsid/keys and
would very likely bootstrap a **new** mon identity against a PVC that
already has an initialized (now orphaned-looking) mon store — undermining
identity preservation. This was not independently re-tested to failure (to
avoid burning a second live Ceph bring-up in the shared dev cluster), but it
follows directly from the ownerReference evidence: the fsid lives in the
`rook-ceph-mon` Secret, not on the PVC's block device in a place Rook's
control plane reads before deciding to `mkfs`.

**Restore mode must therefore orphan-recreate this entire set, not just
PVCs:**

1. Mon + OSD PVCs (by name / by device-set label)
2. `rook-ceph-mon` Secret (fsid + mon-secret + admin-secret)
3. `rook-ceph-mon-endpoints` ConfigMap
4. `rook-ceph-admin-keyring`, `rook-ceph-mons-keyring`, `rook-ceph-config` Secrets
5. `rook-csi-rbd-node`, `rook-csi-rbd-provisioner` Secrets (client keys baked
   consumers rely on)

For pattern restore this maps to: capture these Secrets/ConfigMap alongside
the mon/OSD `PatternDisk`s (small — no VolumeSnapshot needed, just their
`data`), and pre-create them (without an owning `CephCluster`, since it
doesn't exist yet) before creating the restore-mode `TroshkaCeph`/`CephCluster`.

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
3. Pre-create the identity Secrets/ConfigMap listed above from captured
   values (fsid, mon-secret, admin-secret, csi user keys) — **without** an
   owning `CephCluster` (none exists yet).
4. Only then create the `TroshkaCeph`/`CephCluster` CR.

## Recommendation

**Proceed with Strategy A.** No pivot to Strategy B (explicit
`troshka-ceph-osd-*` PVCs via `build_osd_pvcs`) or Strategy C (abort) needed.
Downstream tasks should:
- Capture the OSD/mon `PatternDisk`s exactly as designed (§5 of the design doc).
- Additionally capture the small identity Secrets/ConfigMap listed above
  (not full VolumeSnapshots — just their string data) as part of the
  `projectCephCapture` metadata, since fsid/keys live there, not just in the
  raw block devices.
- Recreate PVCs + those Secrets/ConfigMap (unowned) before creating the
  restore-mode `TroshkaCeph`, matching the label scheme above for OSDs.
- `build_osd_pvcs()` in `rook_ceph.py` remains dead code; do not wire it in
  for restore — Rook's own device-set PVC discovery already does the job.
