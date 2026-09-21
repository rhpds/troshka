# Project Ceph in Pattern — Identity-Preserving Thin Capture

**Date:** 2026-09-21  
**Status:** Draft (design)  
**Scope:** Troshka pattern capture/restore for `TroshkaCeph` / `cephClusterNode`. General-purpose (any Ceph consumer in the pattern — nested ODF, VM ceph-clients, etc.). KubeVirt-native projects only (same constraint as project Ceph v1).

---

## 1. Motivation

Patterns today capture VM `storageNode` disks only. Project Ceph (`TroshkaCeph`) is recreated empty on pattern deploy. Nested ODF and any VM that holds Ceph client credentials (fsid, keyring, mon endpoint) break or need manual rewiring.

Catalog and lab authors need **freeze → thin capture → restore with original identity and contents** so anything that used Ceph at capture time can use it again after deploy **without** Troshka knowing every consumer.

---

## 2. Goals and non-goals

### Goals

- Capture **all** project Ceph durable state needed for identity + data: mon PVC + OSD PVCs
- Store artifacts as **thin** as practical (sparse/compressed qcow), not raw `capacityGi`
- Restore into a new project with the **same** fsid, auth, and mon endpoint clients expect (`labIp`)
- **Automatic** reconnect for unknown consumers (no per-consumer secret rewrite list)
- General-purpose — not CCLM-specific
- Quiesce writers during capture for crash-consistent devices
- **Parallelize** independent snapshot/export work to reduce capture wall time
- Gate pattern-deploy consumers on restored Ceph Ready before they power on

### Non-goals

- Logical `rbd export` of individual images as the primary path (thinner, but new cluster identity)
- Rewriting nested ODF / arbitrary client credentials on restore
- Capturing host ODF or non–project-Ceph storage
- troshkad / ocpvirt providers
- Multiple Ceph clusters per project

---

## 3. Approach (chosen)

**Sparse mon + OSD device capture with identity-preserving restore.**

| Layer | Behavior |
|-------|----------|
| Capture | Quiesce → freeze Ceph → VolumeSnapshot each mon/OSD PVC → sparse qcow → S3/RGW as `PatternDisk`s |
| Topology | Keep `cephClusterNode` (`labIp`, capacity, osdCount, networkRef). Clear runtime `projectCeph` stamp from pattern topology |
| Restore | Pre-create mon/OSD PVCs from pattern disks → `TroshkaCeph` **restore mode** (attach existing devices, skip empty bootstrap) → same `labIp`/NAD |
| Consumers | Boot only after restored Ceph is healthy; baked credentials keep working |

**Rejected alternatives**

- New empty Ceph + `rbd import` + consumer rewrite — fails unknown VM clients  
- Force foreign fsid into fresh Rook + image import — unsupported / fragile  
- Full non-sparse OSD dumps — unnecessarily large when devices are lightly used  

---

## 4. Capture → restore → boot order

### Capture (source project)

1. Quiesce writers (stop nested guest VMs / pause consumers; align with existing OCP pattern quiesce).
2. Freeze Ceph I/O (`noout` / flush / stop OSD and mon as needed so block devices are idle).
3. Discover mon + OSD PVCs for `project-ceph` in the project namespace.
4. **In parallel:** VolumeSnapshot each device, then **in parallel:** export each snapshot to sparse qcow and upload (see §7).
5. Register `PatternDisk` rows with Ceph metadata; attach references on pattern topology.
6. Unfreeze / restart Ceph on the **source** project so the live lab recovers.
7. Honor existing `restart_after` for VMs/consumers.
8. Continue/finish VM disk capture as today (ordering: Ceph devices while frozen; VM disks may run after unfreeze if already quiesced, or while still quiesced — prefer completing Ceph export before unfreeze).

### Restore (pattern deploy)

1. Remap topology; **preserve `labIp`**. Remap `networkRef` / `linkedClusters` (fix existing remap gap).
2. **Before** Rook: create mon + OSD PVCs from pattern Ceph disks (correct size/order).
3. Create `TroshkaCeph` in restore mode bound to those PVCs.
4. Wait until Ceph is healthy with original identity.
5. Only then start VMs / nested OCP / other Ceph consumers.
6. Optional: refresh Troshka export secret for mounts; not required for baked client credentials.

**Boot-order rule:** Restored project Ceph Ready **before** any Ceph consumer in the pattern powers on.

---

## 5. Components and contracts

### PatternDisk metadata

Extend existing `PatternDisk` (no separate store):

| Field | Purpose |
|-------|---------|
| `source_kind` | `ceph-mon` \| `ceph-osd` (VM disks remain unset or `vm`) |
| `source_index` | OSD index `0..N-1`; mon uses `0` |
| `source_pvc_name` | PVC name at capture (debug / matching) |
| `virtual_size_bytes` | Full block size for restore PVC requests |
| S3 object | Sparse/compressed qcow2 |

### Topology

- Retain `cephClusterNode` (+ edges) in pattern topology.
- Add capture index on the node or pattern, e.g.:

```yaml
projectCephCapture:
  monDiskId: <PatternDisk.id>
  osdDiskIds: [<id>, ...]   # ordered by source_index
```

- Strip stale `projectCeph` runtime stamp from saved pattern topology (re-stamp after restore Ready if needed for Troshka mounts).

### Operator (`TroshkaCeph`)

- New restore path: spec/annotation listing pre-created mon + OSD PVC names (or `restoreFromPattern: true` + convention).
- **Skip** empty `storageClassDeviceSets` bootstrap when restoring.
- Bind Rook to existing PVCs; keep Multus NAD + `labIp` from topology.
- Teardown continues to delete project Ceph PVCs with the project.

### Backend

- **Capture:** After quiesce/freeze, enumerate Rook mon+OSD PVCs → parallel snapshot/export → `PatternDisk` registration.
- **Deploy:** If pattern has Ceph disks → materialize PVCs → TroshkaCeph restore mode → gate consumer start on Ready.
- **Remap:** `_remap_topology` remaps `cephClusterNode.data.networkRef` and `linkedClusters` / linked cluster ids.

### Backward compatibility

| Pattern content | Deploy behavior |
|-----------------|-----------------|
| `cephClusterNode` + Ceph `PatternDisk`s | Identity-preserving restore |
| `cephClusterNode` only (no Ceph disks) | Empty Ceph bootstrap (today) |
| No `cephClusterNode` | Unchanged |

---

## 6. Freeze, failures, thinness

### Freeze sequence

1. Quiesce consumers.  
2. Ceph: set flags / flush / stop OSDs (and mon if required) so devices are idle.  
3. Parallel snapshot + parallel sparse export.  
4. Restart Ceph on source; resume consumers per `restart_after`.

Exact ceph CLI / Rook stop order is an implementation detail; must leave **source** project healthy after capture.

### Failure modes

| Case | Behavior |
|------|----------|
| Ceph not Ready at capture start | Fail capture |
| OSD count ≠ topology `osdCount` / unexpected PVC set | Fail — no partial Ceph capture |
| Any Ceph device snapshot/export fails | Fail whole Ceph capture; pattern not `available` with partial Ceph |
| Restore PVC size mismatch | Fail deploy before Rook start |
| Restored Ceph never healthy | Block deploy; do not start Ceph consumers |
| `cephClusterNode` without Ceph disks | Empty bootstrap (legacy) |

### Thinness

- Stored size ≈ used/allocated bytes on mon+OSD devices after sparse + compress — **not** `capacityGi`.
- Docs/UI: warn that a full pool yields a large pattern; lightly used pools stay small.
- Not as minimal as per-image `rbd export`; identity preservation is the trade-off.

### Success criteria (deploy from pattern)

- Cluster comes up with capture-time identity (fsid verifiable).
- Mon reachable at topology `labIp` (clients’ configured endpoint).
- RBD pool contents present; a client using capture-time keyring/fsid/mon works without rewrite.
- Nested ODF / other baked consumers function after boot gate.

---

## 7. Parallelism (capture time)

Independent devices do not share a serial export pipeline.

**Required**

1. **Parallel VolumeSnapshots** — create snapshots for mon + all OSDs concurrently (wait for all Ready).
2. **Parallel export/upload** — once a snapshot is Ready, start its temp-PVC → `qemu-img` sparse convert → rclone/S3 without waiting for sibling devices (bounded concurrency, e.g. min(N, 3–4) to avoid crushing host disk/network).
3. **Do not** serialize “snapshot all, then export all” unless the platform requires it; prefer per-device pipeline: `snapshot → export → upload` as soon as each snapshot is ready.
4. VM disk capture may run **in parallel with Ceph device exports** only if quiesce/freeze invariants hold (Ceph devices still frozen/idle until their exports finish). Prefer: finish all Ceph device exports → unfreeze Ceph → then VM exports if VM snapshotting needs Ceph unfrozen; or snapshot VMs while Ceph frozen if that matches existing OCP quiesce.

**Not parallelized (ordering constraints)**

- Quiesce before any Ceph snapshot  
- Unfreeze Ceph only after all Ceph device exports succeed (or after failed capture cleanup)  
- Restore: all Ceph PVCs + Rook healthy before consumer power-on  

---

## 8. Testing

- Unit: PatternDisk metadata; topology capture block; remap of `networkRef` / linked clusters; restore-mode spec builder.
- Operator: restore mode skips empty device sets; binds given PVCs; teardown deletes restore PVCs.
- Integration (dev cluster): small `capacityGi` project → write known RBD content + client keyring on a VM → capture pattern → deploy → verify fsid, content, client access without secret rewrite.
- Failure: missing OSD PVC at capture → pattern stays non-available for Ceph path; partial export aborts.

---

## 9. Open implementation details (non-blocking for design)

- Exact Rook PVC name discovery for mon vs `osd-set-*`  
- Whether mon must be stopped vs only OSDs for consistent snapshot  
- Concurrency cap default and config knob  
- DB column vs JSON metadata for `source_kind` / `source_index`  

These are settled in the implementation plan / spikes, not design forks.

---

## 10. Summary

Project Ceph becomes a first-class pattern citizen: **sparse mon+OSD disks**, **identity-preserving restore**, **consumer-agnostic**, **parallel capture**, boot-gated so restored Ceph is ready before anything that depended on it starts.
