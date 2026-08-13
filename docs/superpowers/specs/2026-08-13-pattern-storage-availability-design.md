# Pattern Storage Availability: Sync, Placement, and Deploy Source-Selection

**Date:** 2026-08-13
**Status:** Design — approved for planning
**Related:** [`2026-08-12-pattern-capture-v2-design.md`](2026-08-12-pattern-capture-v2-design.md)

## Problem

Deploying a KubeVirt pattern captured by Pattern Capture v2 fails with CDI
importer errors that surface in the UI as
`error — Unable to connect to s3 data source: cou…`.

The message is misleading twice over:

1. It is not a *connection* failure. The CDI importer reaches the endpoint and
   receives a clean HTTP **404 NoSuchKey**. The truncation (`deploy_service.py`
   clips the CDI condition message to 40 chars) hides the real words:
   *"could not get s3 object"*.
2. The real failure is **missing objects**, not connectivity.

### Observed failure (prod, ocpv06.dal10)

- Pattern `OSAC3 v2-pattern` (`01719f55`) captured on ocpv06 at 20:52:32 → 7
  disks written to ocpv06's **local RGW / OBC** bucket
  (`troshka-patterns-931eba89…`), with `PatternLocation(state="synced",
  provider=ocpv06)` rows.
- Deploy started 83 seconds later, auto-placed back on ocpv06.
- CDI importers pulled from **central S4** (`troshka-images` @
  `troshka-s4-troshka.apps.ocpv-infra01`) → all 7 objects returned 404 →
  importer pods `CrashLoopBackOff`, golden DataVolumes stuck `ImportInProgress`.

### Root cause

This is a **half-finished migration**. Pattern Capture v2 (shipped 2026-08-12)
moved capture to the local RGW/OBC and records `PatternLocation` rows, but the
deploy and operator sides were never updated:

- `deploy_service.py._check_central_source` (`:1712`) checks only *primary*
  (`troshka-images` @ S4) then *gold*, and defaults to primary when the object
  is in neither. It never consults `PatternLocation` or the cluster OBC.
- The operator's `_resolve_disk_s3` (`operator/handlers/vm.py:196`) routes only
  *primary-vs-central*. The OBC is attached to the CR as a nested `obcConfig`
  (`kubevirt.py:1012`) but is **never selected**.
- **No OBC→central sync exists.** Capture writes only to the source cluster OBC;
  nothing copies disks up to central `troshka-images`.
- Placement (`placement.py:162`) is capacity-only and ignores where a pattern's
  disks live.

### Current data (prod)

5 patterns, 26 disks. Only OSAC3 (7 disks) is OBC-based and broken. The other 4
patterns (19 disks) predate v2, live in central `troshka-images`, and deploy
fine. `PatternLocation` currently has exactly 7 rows (OSAC3's).

## Terminology

Three S3 endpoints exist; the code names are inverted from the bug report.

| This spec | Code | Function | Bucket |
|---|---|---|---|
| **central S4** | primary `s3_config` | `_get_s3_config()` (provider `type='s3'`) | `troshka-images` |
| gold / central library (**out of scope**) | `central_s3_config` | `_get_readonly_s3_config()` (`type='s3_readonly'`) | `troshka-gold-images` |
| **local RGW / OBC** | `cluster_s3` | `get_cluster_s3_config(db, provider_id)` | per-cluster OBC bucket |

"central S4" throughout this spec = the **primary** `troshka-images` bucket. The
`s3_readonly` / `troshka-gold-images` central-library feature is untouched.

## Goals

1. A captured pattern is deployable on its **source cluster immediately** (pull
   from local RGW) and on **any other cluster** once synced to central S4.
2. Placement never sends a deploy to a cluster where the pattern's storage is
   not fully available.
3. Deploy verifies **all** disk objects exist before launching CDI importers —
   fail fast with an actionable reason instead of 404 crash-loops.

## Non-goals

- Central S4 raw capacity provisioning (infra task).
- Central S4 eviction / LRU cache management (possible follow-up).
- Any change to the gold `s3_readonly` central-library feature.
- Non-KubeVirt (troshkad) capture/deploy paths beyond keeping them working.

## Approved decisions

- **Sync timing:** eager on capture (OBC→central S4). Pattern cross-cluster-
  deployable only once sync completes; source-cluster deploy works immediately.
- **Placement:** filter to storage-ready clusters, then existing capacity rules.
- **Central S4 capacity:** pre-flight guard before sync (fail with clear error);
  no eviction.
- **Not-ready deploy:** fail fast with an actionable reason.
- **Sync orchestration:** backend RQ worker orchestrates; bytes move via an
  rclone Job on the source cluster.
- **Capacity measurement:** config quota + summed tracked central sizes (no live
  RGW-admin query).
- **Backfill:** proactive one-time — backfill central `PatternLocation` rows for
  legacy patterns already in central; enqueue one sync job for OSAC3.

## Design

### 1. Data model & readiness predicate

`PatternLocation` (existing: `pattern_disk_id`, `provider_id`, `s3_key`,
`state`, `synced_at`, `size_bytes`, `error_message`) gains:

- **`location_type`** — new column, `"obc"` (default) or `"central"`.
  - `location_type="obc"` → `provider_id` = the cluster whose RGW holds the disk.
  - `location_type="central"` → `provider_id` **NULL**; means present in central
    S4 (`troshka-images`). Requires making `provider_id` nullable.
- **States:** `syncing` (copy in flight) → `synced` (verified present) →
  `error`. Capture continues to write `obc` rows directly as `synced`.

Alembic migration: add `location_type` (default `"obc"`, backfill existing rows
to `"obc"`), make `provider_id` nullable. FK columns use
`postgresql.UUID(as_uuid=False)`.

**Readiness predicate** — one helper reused by placement and deploy:

```
def pattern_disk_source_for_cluster(db, pattern_disk, target_provider_id):
    # returns "obc" if a synced obc location on target_provider_id exists,
    #         "central" if a synced central location exists,
    #         None otherwise.

def pattern_ready_on_cluster(db, pattern_id, target_provider_id) -> bool:
    # True iff every PatternDisk resolves to a non-None source.
```

"All disks or not ready" — no partial deploys.

### 2. Eager capture → central S4 sync

**Trigger:** at the end of successful capture (`pattern_service.py:~831`, after
PatternDisks + obc locations commit), enqueue RQ job
`sync_pattern_to_central(pattern_id)`. Capture returns immediately.

**Why a Job on the source cluster:** the OBC endpoint is an in-cluster `.svc`
address unreachable from the infra01 workers. The RQ worker *orchestrates*; the
bytes move via a Kubernetes **rclone Job on the source cluster** (OBC remote →
central S4 remote), created through the kubevirt provider's k8s client. This
reuses the Capture v2 rclone pattern.

**`sync_pattern_to_central(pattern_id)` steps:**

1. **Capacity guard:** `sum(size_bytes of synced central locations) +
   pattern.total_size_bytes` vs configured `central_s4.max_bytes`
   (new `config.yaml` key). If exceeded → mark central rows `error` with a clear
   message, set pattern-level sync error, stop. No partial copy.
2. Create `PatternLocation(location_type="central", provider_id=NULL,
   state="syncing")` rows for each disk (skip disks already `synced` central).
3. Create the rclone Job on the source cluster; poll to completion with a
   bounded timeout (mirror `_wait_for_datavolume`'s 3600s).
4. Success → flip central rows to `synced` (+ `synced_at`, `size_bytes`); notify.
   Failure/timeout → `error` + message surfaced on the pattern.

**Idempotency:** already-`synced` central rows → no-op. rclone copy is
re-runnable.

### 3. Deploy source-selection, operator OBC routing, pre-flight

**3a. Backend per-disk source selection** — replace `_check_central_source` /
rework `_resolve_pattern_disk` (`deploy_service.py:1712-1766`). Using the
**target** provider (the host the project is placed on) and the readiness
predicate:

- synced `obc` location on target provider → `diskSource = "obc"`.
- else synced `central` location → `diskSource = "central"`.
- else → **fail the deploy** (correctness backstop; placement should prevent it).

Set `data["diskSource"] = "obc" | "central"` (replacing boolean `centralSource`)
and `data["resolvedS3Path"]`.

**3b. Pre-flight verification** — before the CR is created, `head_object` every
pattern disk against its chosen source's real endpoint/bucket. Any miss → fail
fast with `"pattern disk <label> not found in <source> — storage not ready"`,
**before** launching CDI. This is the live gate against DB/reality drift.

**3c. Operator routing** (`operator/handlers/vm.py:_resolve_disk_s3` + CR in
`kubevirt.py:1003-1043`):

- Carry per-disk source into the CR (`patternImage.source: "obc"|"central"`).
- Extend `_resolve_disk_s3` to return the OBC config + `s3-obc-credentials` when
  `source=="obc"`. The `obcConfig` and secret are already wired into the CR and
  namespace; only the selection branch is missing.

Net effect for the observed failure: OSAC3 on ocpv06 resolves every disk to
ocpv06's OBC, pre-flight passes, CDI pulls locally instead of 404ing.

### 4. Placement storage-awareness

- Thread the project's pattern-disk set (from topology) into
  `find_available_host` (`placement.py:162`) and `_deploy_resolve_host`
  (`deploy_service.py:3816`).
- New candidate filter: a host is eligible only if `pattern_ready_on_cluster`
  is true for its provider (every disk synced on that provider's OBC **or** in
  central S4). Survivors keep the current sort (fewest in-flight, then most free
  RAM).
- Fail fast with a **distinct** reason when no candidate survives:
  - readiness the only blocker → *"pattern storage still syncing to central S4 —
    try again shortly"*.
  - capacity the only blocker → *"no cluster has capacity for this project"*.
- Non-pattern deploys skip the filter — behavior unchanged.

### 5. Backfill

Proactive, one-time at rollout:

- **Legacy patterns:** for every `PatternDisk` whose object is present in central
  `troshka-images` and has no central location row, create
  `PatternLocation(location_type="central", provider_id=NULL, state="synced",
  synced_at=now)`. ~19 rows for the 4 legacy patterns. Instant, DB-only.
- **OSAC3:** enqueue `sync_pattern_to_central` once (~5 GB OBC→central).

This makes source-selection and the readiness predicate a **single uniform
PatternLocation-driven path** with no legacy `_check_central_source` fallback;
the pre-flight HEAD remains the backstop.

### Error handling

- Sync errors → pattern-level field + `PatternLocation.error_message`, shown in
  the patterns UI.
- Deploy pre-flight / placement failures → existing `deploy_error` /
  deploy-progress channel with the distinct messages above.

## Testing

SQLite suite (selection/predicate logic is pure and easily unit-tested):

- **Readiness predicate:** all-synced-obc, all-synced-central, mixed, partial
  (one disk missing → not ready), no-locations.
- **Source selection:** target==source provider → `obc`; only central synced →
  `central`; neither → raises.
- **Placement filter:** ready vs unready clusters; readiness-blocker and
  capacity-blocker produce the correct distinct messages.
- **Sync worker:** capacity-guard rejection; `syncing`→`synced` transition;
  idempotent re-run; timeout → `error`.
- **Operator `_resolve_disk_s3`:** `obc` / `central` / library branches return
  the correct secret + config.
- **Pre-flight:** missing object → failure before CR creation (mock
  `head_object`).
- **Backfill:** legacy disk present in central → central row created; OSAC3 →
  sync enqueued.

## Files touched (anticipated)

- `src/backend/app/models/pattern_location.py` — `location_type`, nullable
  `provider_id`.
- `src/backend/alembic/versions/` — migration.
- `src/backend/app/services/pattern_service.py` — enqueue sync on capture.
- `src/backend/app/services/` — new `pattern_sync.py` (worker +
  `sync_pattern_to_central`, capacity guard, rclone Job) + readiness helpers.
- `src/backend/app/services/deploy_service.py` — source-selection, pre-flight,
  placement threading.
- `src/backend/app/services/placement.py` — readiness filter.
- `src/backend/app/services/providers/kubevirt.py` — per-disk source in CR.
- `src/operator/handlers/vm.py` — `_resolve_disk_s3` OBC branch.
- `src/backend/config/config.yaml` — `central_s4.max_bytes`.
- Backfill: one-time script or startup task.
- Tests under `src/backend/tests/`.
