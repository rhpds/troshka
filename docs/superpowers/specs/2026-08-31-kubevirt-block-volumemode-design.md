# KubeVirt-native disks: migrate from Filesystem to Block volumeMode

**Status:** Draft / proposed
**Date:** 2026-08-31
**Author:** Patrick Rutledge (with Claude)
**Scope:** `src/operator` (KubeVirt-native provider only). No change to the libvirt/troshkad or OCP Virt (AgnosticD) providers.

## Problem

KubeVirt-native disk PVCs (golden cache imports, per-VM clones, blank data disks) are all created in **`Filesystem`** volumeMode. In Filesystem mode a PVC holds a `disk.img` file inside a filesystem, so the PVC must be larger than the nominal disk to fit the raw image plus filesystem overhead. The code compensates with a fixed headroom heuristic:

```python
# helpers/kubevirt.py — build_datavolume_from_s3 (legacy branch), build_clone_datavolume
request_gb = max(size_gb + 10, int(size_gb * 1.2), source_size_gb)
```

(originally `max(size_gb+5, size_gb*1.1)`, bumped to 20%/+10 GB in commit `76eaf044` because Filesystem CDI imports were running out of space.)

Consequences:
- A user-requested **80 GiB** data disk is provisioned as a **96 GiB** PVC (`80 * 1.2`). The guest sees ~96 GiB, not 80.
- An 11 GB ISO CDROM clone lands at ~20–30 GiB.
- Every clone inherits the golden's import padding (a clone must be `>=` its source), so disks can never be smaller than the padded golden.

In **`Block`** volumeMode the PVC *is* the raw disk device — no `disk.img`, no filesystem, no overhead. A requested 80 GiB disk becomes an 80 GiB PVC, exactly. Block is also KubeVirt's recommended mode for VM disks (better performance) and, with `ReadWriteMany` Block, is a prerequisite for live migration.

## Goal

Provision all KubeVirt-native VM disk PVCs (golden imports, clones, blank disks) in **Block** volumeMode so PVC size == requested disk size, eliminating the sizing overhead, and rewrite the three disk-manipulation jobs that currently depend on a Filesystem `disk.img`.

Non-goals: in-place migration of existing Filesystem PVCs (see Compatibility); changing other providers; enabling live migration (separate follow-up, though this unblocks it).

## Current state / why Filesystem today

`Block` volumeMode is used nowhere in the operator today; everything is Filesystem mounts. Three paths hard-depend on a `disk.img` file inside a mounted filesystem:

| Path | Location | Current mechanism |
|------|----------|-------------------|
| **recert job** (regenerates OCP certs before first boot — critical for every OCP deploy) | `helpers/kubevirt.py::build_recert_job` (~673); scripts ~700–820 | Mounts PVC at `/rhcos`, `/bastion` (Filesystem `volumeMounts`); `losetup -f --show /rhcos/disk.img` → `kpartx` → mount partitions |
| **guestfish job** (guest customization via `guestfishCommands`) | `handlers/vm.py::_run_guestfish_job` (~556); cmd ~586 | `guestfish --rw -a /disk/disk.img` |
| **pattern capture** (VM disk → qcow2 pattern) | `helpers/patterns.py` (~85); `build_temp_pvc_from_snapshot` (~23) | `qemu-img convert -f raw -O qcow2 /disk/disk.img /scratch/disk.qcow2` |

These were written around loop-mounting a `disk.img` file, which is the likely reason Filesystem was chosen. This is the bulk of the migration work and the primary risk.

## Design

### 1. PVC/DataVolume creation → Block

Add `volumeMode: "Block"` and size to the exact requested size (no overhead multiplier) in:

- `helpers/kubevirt.py::build_datavolume_from_s3` (golden import): `spec.storage`/`spec.pvc` gets `volumeMode: Block`. Golden size = `source_size_gb` (the real image size) with **no** `*1.2/+10`. Block import needs no filesystem overhead. Keep a tiny safety margin only if empirically required (validate during implementation — see Testing).
- `helpers/kubevirt.py::build_clone_datavolume` (per-VM clone): `volumeMode: Block`; target = `max(requested_size_gb, golden_capacity_gb)` — the requested disk size, floored at the source golden's capacity (CDI still validates target >= source). No `*1.2/+10`.
- `helpers/kubevirt.py::build_blank_pvc` (blank data disk): `volumeMode: Block`; size = requested size exactly.
- Pattern temp PVC `helpers/patterns.py::build_temp_pvc_from_snapshot`: Block (it feeds `qemu-img convert`, which reads a block device fine).

Access mode: keep `ReadWriteOnce` for now (matches today; no migration behavior change). RWX Block is a separate live-migration follow-up. The StorageProfile for `ocs-storagecluster-ceph-rbd-virtualization` supports both `RWO Block` and `RWX Block`.

### 2. Rewrite disk-manipulation jobs for block devices

For each job, attach the PVC as a **`volumeDevices`** entry (raw block device at a `devicePath`, e.g. `/dev/xvda`) instead of a Filesystem `volumeMounts`, and operate on the device directly:

- **recert** (`build_recert_job`): drop `losetup /…/disk.img`; run `kpartx -av /dev/<rhcos>` / `/dev/<bastion>` directly on the block device, then mount partitions as today. The scratch/output volumes stay Filesystem `emptyDir`. Preserve the existing xfs_repair fallback and cleanup logic. The pod needs privileged/`SYS_ADMIN` (already required for losetup/kpartx today).
- **guestfish** (`_run_guestfish_job`): `guestfish --rw -a /dev/<disk>` against the block device instead of `/disk/disk.img`.
- **pattern capture** (`patterns.py`): `qemu-img convert -f raw -O qcow2 /dev/<disk> /scratch/disk.qcow2` (read the block device instead of `disk.img`).

Add a small helper to build a `volumeDevices` attachment consistently (name, devicePath) so the three jobs share one shape.

### 3. KubeVirt VM disk wiring

`build_kubevirt_vm` disk volumes already reference PVCs by `claimName`; KubeVirt uses the PVC's volumeMode natively, so no domain change is expected. Verify the `disk`/`bus` mapping still emits a `disk` (not `cdrom`/`lun`) device for Block PVCs (it should).

## Compatibility / migration

- **No in-place conversion.** Existing Filesystem PVCs keep working; only newly created disks are Block. A project must be redeployed (or its disks re-provisioned) to become Block. Goldens are cache — delete the cached Filesystem golden so it re-imports as Block on next use.
- **Mixed mode during transition** is fine: recert/guestfish/capture jobs must detect the attached disk's mode, OR — simpler — we cut over entirely and require redeploy. Decision: cut over (new disks Block, jobs assume Block); document that in-flight Filesystem projects must be redeployed before the next recert/guestfish/capture runs against them. (Alternative: dual-path the jobs on `disk.img` existence — more code, avoids forced redeploy. Choose during implementation based on how many live Filesystem projects exist.)

## Risks

- **recert is critical** — it runs on every OCP deploy before first boot. A regression breaks all OCP deploys. Highest test priority.
- Block-device partition tooling (`kpartx`, `xfs_repair`, mounts) must behave identically on `/dev/<x>` as it did on a loop device — generally yes, but validate.
- CDI import to Block: confirm the S3 importer writes raw to the block device and reports the right size (no `disk.img`).
- `troshka-tools` image must contain the block tooling (it already has `qemu-nbd`, `kpartx`, `losetup`, `qemu-img`).

## Testing

Unit (operator suite, `src/operator/tests`):
- `build_datavolume_from_s3` / `build_clone_datavolume` / `build_blank_pvc` emit `volumeMode: Block` and exact sizes (no `*1.2/+10`).
- Job builders emit `volumeDevices` (block) not Filesystem `volumeMounts` for the disk, with correct `devicePath`, and the scripts reference `/dev/<x>` not `disk.img`.

E2E on ocpvdev01 (dedicated branch):
1. Fresh deploy: golden import (Block) succeeds; per-VM clone Block; blank disk Block; **requested 80 GiB disk → 80 GiB PVC**; VMs boot.
2. **recert** runs and the SNO boots with regenerated certs (the make-or-break test).
3. `guestfishCommands` apply correctly.
4. Pattern capture → qcow2 → deploy from that pattern round-trips.
5. VolumeSnapshot-based flows still work with Block source.

## Rollout

- Dedicated branch; full operator + e2e test pass before merge.
- Deploy via `deploy-full.sh` (operator image + CRDs + restart). No CRD schema change expected.
- After deploy, delete stale Filesystem goldens from `troshka-cache` so images re-import as Block.

## Open questions

1. Dual-path the jobs (auto-detect `disk.img` vs block) to avoid forcing redeploys, or hard cut-over? (Depends on count of live Filesystem projects.)
2. Move to `RWX` Block now (enables live migration) or stay `RWO` and do migration separately? (Recommend separate.)
3. Any minimum Block import safety margin needed, or is exact-size safe? (Determine empirically in Testing step 1.)
