# Pattern Capture v2: Local-First with Distributed RGW Sync

## Problem

Pattern capture on KubeVirt writes qcow2 images to central S4 (a 200 GiB Ceph RadosGW container on infra01) through an OCP Route. Upload speed is 15-80 MiB/s, making a 1 TB pattern take 4-8 hours. The capture blocks the user for the entire duration. S4 is a single point of failure in a single DC (dal12), and its 200 GiB PVC cannot hold more than a handful of patterns.

## Solution

Replace S4 with each cluster's existing Ceph RGW (via ObjectBucketClaims). Capture writes to the local cluster's RGW at ~275 MiB/s via ClusterIP, then background sync Jobs distribute the pattern to all other clusters using rclone S3-to-S3 streaming at ~494 MiB/s — no scratch disk required.

## PoC Results (validated 2026-08-12 on ocpv06/ocpv03/ocpv07)

### Local Capture — 1.2 TiB bastion disk (295 GiB qcow2)

| Phase | Time | Speed |
|-------|------|-------|
| qemu-img convert (1.11 TiB raw → 295 GiB qcow2) | 10 min | — |
| Upload to local RGW (ClusterIP) | 18 min | 275 MiB/s |
| **Total capture** | **29 min** | — |

### Cross-Cluster Sync — rclone S3-to-S3 (no scratch disk)

| Path | Time | Speed |
|------|------|-------|
| Same-DC (dal10 → dal10) via rclone | 10 min | 494 MiB/s |
| Same-DC (dal10 → dal10) via aws s3 cp + scratch | 33 min | ~280 MiB/s |
| Cross-DC (dal10 → wdc06) via aws s3 cp + scratch | 31 min | ~297 MiB/s download |

### Optimal S3 Settings (validated by benchmark)

| Setting | Value | Reason |
|---------|-------|--------|
| RGW pods per cluster | 3 | Linear scaling to ~289 MiB/s; 6 pods showed diminishing returns |
| aws s3 cp concurrent requests | 7 | Best throughput with 3 RGW pods (294 MiB/s) |
| Multipart chunk size | 256 MB | Chunk size has minimal impact; 256 MB balances memory vs overhead |
| Sync tool | rclone | 3.3x faster than aws s3 cp, no scratch disk needed |
| rclone s3-chunk-size | 256M | Matches aws cli optimum |
| rclone s3-upload-concurrency | 7 | Matches aws cli optimum |

### vs Current S4 Path

| | Current (S4) | Pattern Capture v2 |
|---|---|---|
| Capture time (1 TB) | 4-8 hours | **29 min** |
| Deployable on source cluster | After S4 upload done | **Immediately after capture** |
| Sync to all clusters | N/A (S4 is single store) | **~10 min per cluster (parallel)** |
| Total: capture + sync everywhere | 4-8 hours | **~39 min** |
| Storage capacity | 200 GiB PVC | 89-189 TiB per cluster |
| Single point of failure | S4 on infra01 | None — distributed |

## Architecture

### Capture Flow

```
Phase 1 (fast, ~29 min):
  VolumeSnapshot → temp PVC → qemu-img convert → local Ceph RGW (ClusterIP)
  Pattern marked "available" — deployable on source cluster immediately

Phase 2 (async, background, ~10 min per cluster in parallel):
  Source cluster RGW (Route) → rclone S3-to-S3 → target cluster RGW (ClusterIP)
  Pattern marked "synced" per-cluster — deployable everywhere
```

### Deploy Path

```
1. Look up pattern disks → get s3_key
2. Check pattern_locations for target cluster
3. If synced → DataVolume source = target cluster's local RGW endpoint
4. If missing → enqueue sync Job, return "syncing" to frontend
5. Golden PVC clone path unchanged (CDI smart clone from local RGW)
```

## Components

### 1. ObjectBucketClaim per Cluster

Every KubeVirt cluster gets an OBC in `troshka-operator` namespace:

```yaml
apiVersion: objectbucket.io/v1alpha1
kind: ObjectBucketClaim
metadata:
  name: troshka-patterns
  namespace: troshka-operator
spec:
  generateBucketName: troshka-patterns
  storageClassName: ocs-storagecluster-ceph-rgw
```

The OBC auto-provisions:
- A bucket on the local Ceph RGW
- An access/secret key Secret (`troshka-patterns`) in the same namespace
- A ConfigMap with bucket name and RGW endpoint

No ODF root configuration changes. All resources in `troshka-operator` namespace.

### 2. RGW Scaling (per cluster)

Patch the StorageCluster to scale RGW and prevent reconciliation of our customizations:

```yaml
spec:
  managedResources:
    cephObjectStores:
      gatewayInstances: 3
      reconcileStrategy: init
```

Then patch CephObjectStore directly for resources:

```yaml
spec:
  gateway:
    instances: 3
    resources:
      requests:
        cpu: "4"
        memory: "8Gi"
      limits:
        cpu: "8"
        memory: "16Gi"
```

- `reconcileStrategy: init` — OCS operator deploys initially but won't overwrite our customizations
- 3 RGW pods gives ~289 MiB/s (linear scaling from 1 pod at 91 MiB/s)
- 6 pods showed diminishing returns on the tested clusters (7 Ceph nodes)
- Resource bump from 2cpu/4Gi to 4cpu/8Gi per Red Hat guidance for large-object workloads

### 3. rclone in troshka-tools Image

Add rclone to the `troshka-tools` container image (single static binary, ~50 MB):

```dockerfile
RUN curl -sO https://downloads.rclone.org/current/rclone-current-linux-amd64.zip && \
    unzip -q rclone-current-linux-amd64.zip && \
    cp rclone-*/rclone /usr/local/bin/ && \
    rm -rf rclone-*
```

Used for sync Jobs (S3-to-S3 streaming, no scratch disk).

### 4. Export Job Changes

The capture export Job changes its S3 target from S4 to local RGW:

```bash
# Before (S4 via Route — slow)
aws s3 cp /scratch/disk.qcow2 s3://troshka-images/<path> \
  --endpoint-url https://s4-troshka-images.apps.ocpv-infra01.dal12...

# After (local RGW via ClusterIP — fast)
aws s3 cp /scratch/disk.qcow2 s3://<obc-bucket>/<path> \
  --endpoint-url http://rook-ceph-rgw-ocs-storagecluster-cephobjectstore.openshift-storage.svc:80
```

S3 settings added to export Jobs:
```bash
aws configure set s3.multipart_chunksize 256MB
aws configure set s3.max_concurrent_requests 7
```

### 5. Sync Job (New)

After capture completes, the backend fans out sync requests to all other clusters. Each target cluster's operator creates a sync Job:

```bash
rclone copyto "source:${SOURCE_BUCKET}/${S3_KEY}" \
  "target:${LOCAL_BUCKET}/${S3_KEY}" \
  --s3-chunk-size 256M \
  --s3-upload-concurrency 7 \
  --transfers 1
```

- No scratch PVC — rclone streams through memory (~1.8 GiB for 7 × 256MB chunks)
- Source config: RGW Route URL + source cluster's OBC credentials
- Target config: local RGW ClusterIP + local OBC credentials
- Progress reported via CR status (same pattern as capture)

Credential management:
- Each cluster's OBC credentials are auto-provisioned locally
- Source cluster credentials stored as `sync-source-{cluster_id}` Secrets in `troshka-operator` namespace on each target cluster
- Backend provisions these Secrets when a new KubeVirt provider is added

## Data Model

### New Table: `pattern_locations`

```
pattern_locations
  id              UUID PK
  pattern_disk_id UUID FK → pattern_disks.id
  provider_id     UUID FK → providers.id  (the KubeVirt cluster)
  s3_key          String                   (key in that cluster's OBC bucket)
  state           Enum: syncing, synced, error
  synced_at       DateTime (nullable)
  size_bytes      BigInteger
  error_message   Text (nullable)
```

Tracks which pattern disks exist on which clusters.

### Modified Table: `patterns`

- Add `source_provider_id` (FK → providers.id) — which cluster the pattern was captured on
- `visibility` extended: `private`, `shared`, `public`
- `state` unchanged: `creating → capturing → available → error`

### Existing Table: `pattern_disks`

- `s3_key` stays — canonical key path, same across all clusters
- No S4-specific changes needed

### Existing Table: `pattern_shares`

- Unchanged — user-to-user sharing

## Visibility and Distribution

| Level | Who can see/deploy | Sync behavior |
|-------|-------------------|---------------|
| `private` | Owner only | Stays on source cluster. Synced on-demand if owner deploys elsewhere. |
| `shared` | Owner + shared users | Synced on-demand when a shared user deploys on another cluster. |
| `public` | All users (admin-set) | Auto-sync to all clusters immediately. |

Promotion flow:
- User captures → `private`, single location (source cluster)
- User shares with specific users → `shared` via `PatternShare`, no sync
- Admin sets `public` → triggers background sync to all clusters
- Admin uploads base ISO/disk → stored as `public`, auto-synced everywhere

Private patterns don't consume storage on 9 clusters. Only public/gold images replicate everywhere.

## S4 Migration

One-time migration to move existing patterns from S4 to the new distributed RGW:

1. For each existing pattern in S4:
   a. Determine the "home" cluster (from `source_project.host` or assign to a default cluster)
   b. Run a migration Job on the home cluster: `rclone copyto s4:bucket/key local-rgw:bucket/key`
   c. Create `pattern_locations` record
2. For gold/library images:
   a. Sync to all cluster RGWs (they're public)
   b. Create `pattern_locations` records for each cluster
3. Update backend S3 config to use OBC credentials instead of S4
4. Decommission S4 deployment on infra01

Migration can run in background with no downtime — the deploy path checks `pattern_locations` first, falls back to S4 config until migration completes.

## Operator Changes

### OBC Management

Operator creates OBC on startup if it doesn't exist:
- Check for `troshka-patterns` OBC in `troshka-operator` namespace
- If missing, create it
- Read credentials from the generated Secret
- Store local S3 config (endpoint, bucket, credentials) in operator memory

### Sync Handler

New handler triggered by `troshka.redhat.com/sync-request` annotation:
- Parses sync request JSON (source endpoint, credentials secret, s3_key, size)
- Creates a K8s Job using `troshka-tools` image with rclone
- Polls Job status and reads progress
- Reports completion/failure via CR status
- Backend polls status and updates `pattern_locations`

### Capture Handler Changes

- Export Jobs target local RGW instead of S4
- S3 credentials from OBC Secret instead of provider S3 config
- Add S3 tuning (256MB chunks, 7 concurrent)
- On capture complete, backend creates `pattern_locations` for source cluster

## Frontend Changes

### Patterns Page

- Sync status badge: "Local" (orange) / "Syncing 3/9" (yellow) / "Available" (green)
- "Publish" button (admin) — sets visibility to `public`, triggers auto-sync
- "Share" button — existing PatternShare flow

### Pattern Detail View

- Per-cluster sync status table
- Manual "Sync to cluster X" button for on-demand sync
- Sync progress bar via WebSocket

### Deploy Modal

- Pattern exists on target cluster → proceed normally
- Pattern syncing → show progress, "Deploy when ready"
- Pattern not on target cluster → "Sync needed" with sync button

### Admin Panel (New)

- Manage public/gold images
- Upload base ISO/disk images directly
- Global sync status across all clusters

## ODF Configuration (per cluster)

All changes are safe and scoped:

```yaml
# StorageCluster patch (manages RGW pod count, prevents reconcile)
spec:
  managedResources:
    cephObjectStores:
      gatewayInstances: 3
      reconcileStrategy: init

# CephObjectStore patch (resource tuning, persists with init strategy)
spec:
  gateway:
    instances: 3
    resources:
      requests: { cpu: "4", memory: "8Gi" }
      limits: { cpu: "8", memory: "16Gi" }
```

- `reconcileStrategy: init` — deploy initially, don't reconcile customizations
- RGW Routes already exist on every cluster (no new Routes needed)
- OBC in `troshka-operator` namespace (no openshift-storage modifications beyond the above)

Clusters verified (2026-08-12):
- 10 clusters across 6 DCs (dfw3, dal10, dal12, dal13, wdc06, wdc07)
- All have ODF 4.20, Ceph RGW, RGW Routes, OBC support
- 89-189 TiB Ceph storage available per cluster
- Troshka is the only RGW consumer (no impact on other workloads)

## Credential Distribution

When the backend triggers a sync, it needs to ensure the target cluster has credentials to access the source cluster's RGW. Flow:

1. Backend stores each cluster's OBC credentials (access key, secret key) in the `providers` table or a new `cluster_s3_config` table
2. When triggering a sync, the backend creates/updates a `sync-source-{source_cluster_id}` Secret in the target cluster's `troshka-operator` namespace via the K8s API
3. The sync Job references this Secret for the rclone source config
4. Credentials are rotated if OBCs are recreated

This is analogous to how the backend currently provisions `s3-credentials` Secrets for capture Jobs.

## Implementation Order

1. **rclone in troshka-tools** — add rclone binary to container image
2. **OBC setup** — operator creates OBC on startup, reads credentials
3. **RGW scaling** — operator applies StorageCluster + CephObjectStore patches on startup
4. **DB migration** — add `pattern_locations` table, `source_provider_id` to patterns
5. **Local capture** — export Jobs write to local RGW instead of S4
6. **Pattern locations** — backend creates location records on capture complete
7. **Deploy path** — switch golden PVC source URL based on cluster + location state
8. **Sync Job** — operator handler for rclone-based S3-to-S3 sync
9. **Sync orchestration** — backend fans out sync requests, tracks per-cluster state
10. **Frontend** — sync status badges, manual sync, publish button
11. **S4 migration** — migrate existing patterns, decommission S4
12. **Visibility/sharing** — public auto-sync, admin panel for gold images
