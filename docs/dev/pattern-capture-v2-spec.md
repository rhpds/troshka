# Pattern Capture v2: Local-First with Async S4 Sync

## Problem

Pattern capture on KubeVirt currently writes qcow2 images directly to central S4 (RadosGW on infra01) through OCP Routes. Upload speed is ~15-80 MiB/s, making a 2 TB pattern take 4-8 hours. The capture blocks the user for the entire duration.

This mirrors the problem troshkad solved for AWS hosts: fast local capture to NVMe pattern buffer, then async S3 upload.

## Architecture

### Current Flow
```
VolumeSnapshot → temp PVC → qemu-img convert → aws s3 cp to S4 (slow, blocks)
```

### Proposed Flow
```
Phase 1 (fast, ~10 min):
  VolumeSnapshot → temp PVC → qemu-img convert → local Ceph RGW (same cluster)
  Pattern marked "available" — deployable on this cluster immediately

Phase 2 (async, background):
  Local Ceph RGW → central S4
  Pattern marked "synced" — deployable on all clusters
```

## Components

### 1. Local Ceph RGW (already exists)

Every ODF cluster has `rook-ceph-rgw-ocs-storagecluster-cephobjectstore` running on port 80 internally. No route needed — export pods access it via ClusterIP.

**Setup per cluster (one-time):**
- Create an ObjectBucketClaim `troshka-patterns` in `troshka-operator` namespace
- OBC auto-provisions a bucket and generates an access/secret key Secret
- Operator reads credentials from the OBC Secret

### 2. Export Job Changes

The export command changes from:
```bash
aws s3 cp /scratch/disk.qcow2 s3://troshka-images/<path> \
  --endpoint-url https://troshka-s4-troshka.apps.ocpv-infra01.dal12.infra.demo.redhat.com
```
to:
```bash
aws s3 cp /scratch/disk.qcow2 s3://troshka-patterns/<path> \
  --endpoint-url http://rook-ceph-rgw-ocs-storagecluster-cephobjectstore.openshift-storage.svc
```

Internal ClusterIP, no TLS overhead, Ceph-native throughput. Expected: ~500 MiB/s to 1 GiB/s.

### 3. Pattern State Machine

```
creating → capturing → available → syncing → synced
                                  ↗ (deployable on source cluster)
                          synced → (deployable on all clusters)
                          error  → (retry)
```

- `available`: images exist in local Ceph RGW on the source cluster. Pattern can be deployed on that cluster immediately.
- `syncing`: background job copying from local RGW to central S4.
- `synced`: images exist in both local RGW and central S4. Deployable everywhere.

### 4. Cross-Cluster Access — Three Options

The fundamental question: how do other clusters access pattern images captured on one cluster?

#### Option A: Keep S4 as Central Store (Sync Model)

Same architecture as troshkad's pattern buffer: fast local capture, async push to central.

```
Capture cluster (local RGW) → [sync job] → Central S4 → [deploy] → Any cluster
```

**Sync implementation options:**

A1. **Operator-side sync Job** — the operator on the source cluster runs a K8s Job that copies from local RGW to S4. All source reads are local (fast), only the S4 write goes through the Route.

A2. **Worker-side sync via operator proxy** — add a `/patterns/{id}/stream` endpoint to the operator. The infra01 worker streams from the source cluster's operator and writes to S4. More hops but centralizes the sync logic.

A3. **Expose RGW via Route** — create an OCP Route for each cluster's RGW. The worker on infra01 accesses it directly with S3 credentials. Simple but adds external exposure of Ceph.

**Pros:** No architecture change to deploy path. Existing golden PVC CDI import from S4 works unchanged. Pattern export/import uses S4 as single source of truth.

**Cons:** S4 is still a bottleneck for multi-cluster deploys. Sync adds delay before cross-cluster deployment. Two copies of every image (local RGW + S4).

**Recommendation if choosing this option:** A1 (operator-side sync Job). The operator already manages Jobs for export, adding a sync Job is natural.

#### Option B: Eliminate S4 — Direct Cross-Cluster Pulls

Each cluster stores its own patterns in its own Ceph RGW. When deploying on a different cluster, CDI pulls directly from the source cluster's RGW.

```
Capture cluster (local RGW) → [deploy] → CDI pulls from source cluster's RGW
```

**Implementation:**
- Expose each cluster's RGW via OCP Route (TLS passthrough)
- Store per-cluster RGW endpoints and credentials in the provider config
- Golden PVC DataVolume source URL points to the source cluster's RGW, not S4
- Pattern metadata stored in DB only (no metadata.json in S3)

**Pros:** No central bottleneck. No sync step. No S4 infrastructure to maintain. Patterns available immediately after capture. Single copy of each image.

**Cons:** Source cluster must be online when deploying on other clusters. If source cluster goes down, patterns captured there are unavailable. RGW exposed externally on each cluster (security surface). Cross-cluster CDI pulls go through OCP Routes (slower than local). Pattern export/import needs rework.

**Mitigation:** Lazy replication — when a pattern is first deployed on a non-source cluster, cache a copy in that cluster's local RGW. Subsequent deploys on that cluster use the local copy.

#### Option C: Hybrid — Local RGW + Lazy Replication (No Central Store)

No S4 at all. Patterns live in the source cluster's RGW. When deployed on another cluster, the operator copies the images to the target cluster's local RGW first, then CDI clones from local.

```
Capture → source cluster's RGW
Deploy on same cluster → local golden PVC clone (fast)
Deploy on different cluster → copy to target RGW → local golden PVC clone
```

**Implementation:**
- Each cluster has its own RGW with OBC
- Operator has a "pre-deploy cache" step: check if golden images exist locally, if not, pull from source cluster's RGW
- Golden PVCs always clone from local RGW (consistent deploy behavior)
- Source cluster's RGW endpoint stored in pattern metadata

**Pros:** All deploys are local-speed after first pull. No central S4 bottleneck. Patterns naturally replicate to where they're used. Source cluster only needs to be online for the first deploy on a new cluster.

**Cons:** First deploy on a new cluster includes a cross-cluster copy (adds time). More storage used overall (copies on each cluster that uses the pattern). Garbage collection needed for unused cached copies.

#### Network Topology

Clusters span two data centers (dal and wdc). Cross-DC bandwidth is limited compared to intra-DC. This affects the architecture choice:

- Same-DC cluster-to-cluster: fast (~1 GiB/s+)
- Cross-DC: slower, variable
- S4 on infra01 (dal12): all wdc clusters pay cross-DC penalty on every upload and deploy

#### Recommendation

**Option C (lazy replication)** is the best fit for a two-DC topology:

- Capture is always local-speed regardless of DC
- First deploy on a same-DC cluster is fast (intra-DC copy)
- First deploy on a cross-DC cluster pays the cross-DC cost once, then all subsequent deploys on that cluster are local
- No central S4 bottleneck — especially important since S4 is in only one DC
- Popular patterns naturally replicate to both DCs over time

**Option A1 (S4 sync)** is the simpler starting point if we want incremental progress — capture is fast, sync is background, and the deploy path stays unchanged. But S4 in dal12 means wdc clusters always pay cross-DC on deploy.

**Start with A1, migrate to C** — get the capture speedup immediately, then eliminate the S4 bottleneck when cross-DC deploy performance becomes a priority.

### 5. Deploy Path Changes

When deploying a pattern:

1. Check if the golden PVC source is available:
   - If deploying on the **source cluster** and pattern is `available` or `synced`: use local RGW URL
   - If deploying on a **different cluster** and pattern is `synced`: use S4 URL
   - If deploying on a **different cluster** and pattern is only `available`: block with "Pattern syncing to central storage, please wait"

2. Golden PVC DataVolume source URL switches between local RGW and S4 based on which cluster and sync state.

### 6. Database Changes

**patterns table:**
- Add `source_cluster_id` (FK to providers) — which cluster has the local copy
- Add `sync_state` enum: `local_only`, `syncing`, `synced`, `sync_error`
- Add `sync_progress` JSON: per-disk sync status

**pattern_disks table:**
- Add `local_s3_key` — key in the source cluster's local RGW bucket
- Add `synced` boolean — whether this disk has been copied to S4

### 7. Operator Changes

**New CRD fields on TroshkaProject:**
- `status.localS3Config` — local RGW endpoint and credentials secret name

**New handler:**
- `_handle_sync` — triggered by `troshka.redhat.com/sync-request` annotation
- Creates a K8s Job that copies each disk from local RGW to S4
- Reports progress via CR status (same pattern as capture)

**ObjectBucketClaim management:**
- Operator creates OBC on startup if it doesn't exist
- Reads credentials from the generated Secret
- Passes local S3 config to export Jobs

### 8. Frontend Changes

**Patterns page:**
- Show sync state badge: "local" (orange), "syncing" (yellow), "synced" (green)
- "Sync to central" button if pattern is `local_only`
- Auto-sync toggle in settings

**Deploy modal:**
- If pattern is `local_only` and target cluster is different: show warning with sync button

## Migration

1. Existing patterns in S4 are already `synced` — set `sync_state = 'synced'` for all existing patterns
2. Deploy OBC to all clusters via operator startup
3. New captures use local-first flow
4. Auto-sync can be enabled per-user or globally

## Performance Estimates

| Phase | Current | v2 |
|-------|---------|-----|
| qemu-img convert (1 TB) | ~15 min | ~15 min (same) |
| Upload to storage | ~4-8 hours (S4 via Route) | ~5-10 min (local RGW) |
| Total capture time | 4-8 hours | ~20-25 min |
| Async S4 sync | N/A | 4-8 hours (background) |
| Deploy on same cluster | Blocked until S4 upload done | Immediate after capture |
| Deploy on other cluster | Blocked until S4 upload done | Blocked until sync done |

## Implementation Order

1. **OBC setup** — operator creates ObjectBucketClaim on startup, reads credentials
2. **Local capture** — export Jobs write to local RGW instead of S4
3. **Pattern state** — add sync_state, mark available after local capture
4. **Deploy path** — switch golden PVC source URL based on cluster + sync state
5. **Sync job** — background copy from local RGW to S4
6. **Frontend** — sync state badges, manual sync button
7. **Auto-sync** — configurable automatic sync after capture
