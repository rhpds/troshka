# Pattern Capture v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace central S4 with distributed Ceph RGW for pattern capture and cross-cluster sync, reducing capture time from 4-8 hours to ~29 minutes for a 1 TB disk.

**Architecture:** Each KubeVirt cluster stores patterns in its own Ceph RGW via ObjectBucketClaim. Capture writes to local RGW via ClusterIP (~275 MiB/s). Background sync Jobs use rclone for diskless S3-to-S3 streaming to other clusters (~494 MiB/s). Backend DB tracks which patterns exist on which clusters via a `pattern_locations` table.

**Tech Stack:** Python 3.11/FastAPI/SQLAlchemy 2 (backend), kopf (operator), rclone (sync), Next.js 15/PatternFly 6 (frontend), PostgreSQL 16, ODF 4.20 Ceph RGW

**Design Spec:** `docs/superpowers/specs/2026-08-12-pattern-capture-v2-design.md`

## Global Constraints

- Cognitive complexity per function must stay at or below 15 (SonarQube S3776)
- `Mapped[type]` + `mapped_column()` syntax for SQLAlchemy models
- UUIDs as strings: `UUID(as_uuid=False), default=lambda: str(uuid.uuid4())`
- FK columns must use `postgresql.UUID(as_uuid=False)`
- Tests use SQLite with type compiler overrides for JSONB/UUID
- CI runs Python 3.11 — add extra trailing values to `time.time()` mocks
- Always use `type="personal"` for user libraries (never `type="user"`)
- Never block HTTP requests with SSH/downloads — use background threads or RQ jobs
- RGW settings: 3 pods per cluster, 7 concurrent requests, 256MB multipart chunks
- rclone for sync (not aws s3 cp) — diskless S3-to-S3 streaming

## PoC-Validated Performance Numbers

| Operation | Time (295 GiB) | Speed |
|-----------|---------------|-------|
| qemu-img convert (1.11 TiB raw → qcow2) | 10 min | — |
| Upload to local RGW (ClusterIP, 3 RGW pods) | 18 min | 275 MiB/s |
| rclone S3-to-S3 sync (same-DC, no scratch disk) | 10 min | 494 MiB/s |
| rclone S3-to-S3 sync (cross-DC dal10→wdc06) | est. ~12 min | est. ~400 MiB/s |
| aws s3 cp + scratch disk (same-DC, for comparison) | 33 min | ~280 MiB/s |

## ODF Configuration (already applied to all clusters)

StorageCluster and CephObjectStore patches were applied during the PoC to all 9 KubeVirt clusters + ocpvdev01:

```yaml
# StorageCluster
spec.managedResources.cephObjectStores:
  gatewayInstances: 3
  reconcileStrategy: init

# CephObjectStore (direct patch, persists with init strategy)
spec.gateway:
  instances: 3
  resources:
    requests: { cpu: "4", memory: "8Gi" }
    limits: { cpu: "8", memory: "16Gi" }
```

---

## File Map

### New Files

| File | Responsibility |
|------|---------------|
| `src/backend/app/models/pattern_location.py` | PatternLocation SQLAlchemy model |
| `src/backend/app/services/pattern_sync_service.py` | Sync orchestration: fan out sync requests, credential distribution |
| `src/backend/app/api/pattern_sync.py` | API endpoints for sync status, manual sync trigger, publish |
| `src/backend/alembic/versions/xxxx_add_pattern_locations.py` | DB migration |
| `src/operator/helpers/obc.py` | OBC management: create, read credentials |
| `src/operator/handlers/sync.py` | Sync Job handler: rclone-based S3-to-S3 |
| `src/operator/images/troshka-tools/Dockerfile` | Add rclone binary |
| `tests/test_pattern_location.py` | PatternLocation model + sync service tests |
| `tests/test_obc.py` | OBC helper tests |

### Modified Files

| File | Changes |
|------|---------|
| `src/backend/app/models/pattern.py` | Add `source_provider_id` column to Pattern |
| `src/backend/app/models/__init__.py` | Register PatternLocation |
| `src/backend/app/schemas/pattern.py` | Add sync status fields to responses |
| `src/backend/app/services/pattern_service.py` | Capture writes to local RGW via OBC, creates pattern_locations on completion |
| `src/backend/app/services/s3_storage.py` | Add `get_cluster_s3_config(provider_id)` for per-cluster OBC config |
| `src/backend/app/services/providers/kubevirt.py` | `_ensure_s3_secret` uses OBC credentials |
| `src/backend/app/api/patterns.py` | Add sync status to list/detail responses |
| `src/backend/app/main.py` | Register pattern_sync router |
| `src/operator/helpers/patterns.py` | `build_export_job` uses OBC config, adds S3 tuning (256MB chunks, 7 concurrent) |
| `src/operator/handlers/project.py` | `_handle_capture` passes OBC S3 config to export jobs |
| `src/operator/helpers/kubevirt.py` | `build_datavolume_from_s3` resolves per-cluster RGW endpoint |
| `src/frontend/src/app/library/patterns/page.tsx` | Sync status badges, publish button |

---

## Task 1: Add rclone to troshka-tools image

**Files:**
- Modify: `src/operator/images/troshka-tools/Dockerfile`

**Interfaces:**
- Produces: `rclone` binary available at `/usr/local/bin/rclone` in troshka-tools container

- [ ] **Step 1: Add rclone install to Dockerfile**

After the existing package installs, add:

```dockerfile
RUN curl -sO https://downloads.rclone.org/current/rclone-current-linux-amd64.zip && \
    unzip -q rclone-current-linux-amd64.zip && \
    cp rclone-*/rclone /usr/local/bin/ && \
    chmod +x /usr/local/bin/rclone && \
    rm -rf rclone-*
```

Add `unzip` to the `dnf install` line if not already present.

- [ ] **Step 2: Verify locally**

```bash
podman build -t troshka-tools-test src/operator/images/troshka-tools/
podman run --rm troshka-tools-test rclone version
```

Expected: rclone version output (v1.75+)

- [ ] **Step 3: Commit**

```bash
git add src/operator/images/troshka-tools/Dockerfile
git commit -m "feat: add rclone to troshka-tools for S3-to-S3 pattern sync"
```

---

## Task 2: Database migration — pattern_locations + source_provider_id

**Files:**
- Create: `src/backend/app/models/pattern_location.py`
- Modify: `src/backend/app/models/pattern.py` (add source_provider_id)
- Modify: `src/backend/app/models/__init__.py` (register PatternLocation)
- Create: `src/backend/alembic/versions/xxxx_add_pattern_locations.py`
- Test: `src/backend/tests/test_pattern_location.py`

**Interfaces:**
- Produces: `PatternLocation` model with `pattern_disk_id`, `provider_id`, `s3_key`, `state` (syncing/synced/error), `synced_at`, `size_bytes`, `error_message`
- Produces: `Pattern.source_provider_id` column (FK to providers.id, nullable)

- [ ] **Step 1: Write failing test for PatternLocation model**

```python
# tests/test_pattern_location.py
from app.models.pattern_location import PatternLocation

def test_pattern_location_create(db):
    loc = PatternLocation(
        pattern_disk_id="test-disk-id",
        provider_id="test-provider-id",
        s3_key="patterns/test/disk.qcow2",
        state="synced",
        size_bytes=1000000,
    )
    db.add(loc)
    db.commit()
    assert loc.id is not None
    assert loc.state == "synced"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/test_pattern_location.py -v
```

Expected: ImportError — `pattern_location` module not found

- [ ] **Step 3: Create PatternLocation model**

```python
# src/backend/app/models/pattern_location.py
import uuid
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base

class PatternLocation(Base):
    __tablename__ = "pattern_locations"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    pattern_disk_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("pattern_disks.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("providers.id", ondelete="CASCADE"),
        nullable=False,
    )
    s3_key: Mapped[str] = mapped_column(String(500), nullable=False)
    state: Mapped[str] = mapped_column(String(20), default="syncing", nullable=False)
    synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    pattern_disk: Mapped["PatternDisk"] = relationship(back_populates="locations")
    provider: Mapped["Provider"] = relationship()
```

- [ ] **Step 4: Add relationship to PatternDisk**

In `src/backend/app/models/pattern.py`, add to `PatternDisk` class:

```python
locations: Mapped[list["PatternLocation"]] = relationship(
    back_populates="pattern_disk", cascade="all, delete-orphan"
)
```

- [ ] **Step 5: Add source_provider_id to Pattern**

In `src/backend/app/models/pattern.py`, add to `Pattern` class after `source_project_id`:

```python
source_provider_id: Mapped[str | None] = mapped_column(
    UUID(as_uuid=False), ForeignKey("providers.id"), nullable=True
)
```

- [ ] **Step 6: Register in models/__init__.py**

Add to imports in `src/backend/app/models/__init__.py`:

```python
from app.models.pattern_location import PatternLocation
```

Add `PatternLocation` to `__all__`.

- [ ] **Step 7: Create Alembic migration**

```bash
cd src/backend && ./venv/bin/python3 -m alembic revision -m "add_pattern_locations"
```

Edit the generated file. `down_revision = "67320038e4ea"`. The upgrade creates the `pattern_locations` table and adds `source_provider_id` column to `patterns`.

- [ ] **Step 8: Run migration and tests**

```bash
cd src/backend && ./venv/bin/python3 -m alembic upgrade head
cd src/backend && ./venv/bin/python3 -m pytest tests/test_pattern_location.py -v
```

- [ ] **Step 9: Commit**

```bash
git add src/backend/app/models/pattern_location.py src/backend/app/models/pattern.py \
  src/backend/app/models/__init__.py src/backend/alembic/versions/*pattern_locations* \
  src/backend/tests/test_pattern_location.py
git commit -m "feat: add pattern_locations table and source_provider_id"
```

---

## Task 3: OBC management in operator

**Files:**
- Create: `src/operator/helpers/obc.py`
- Modify: `src/operator/handlers/project.py` (call OBC setup on startup)

**Interfaces:**
- Produces: `ensure_obc(api, namespace="troshka-operator") -> dict` returning `{"bucket": str, "endpoint": str, "access_key_id": str, "secret_access_key": str}`
- Produces: `get_obc_s3_config(api, namespace="troshka-operator") -> dict | None`

- [ ] **Step 1: Create OBC helper**

```python
# src/operator/helpers/obc.py
import kopf
import kubernetes

OBC_NAME = "troshka-patterns"
RGW_ENDPOINT = "http://rook-ceph-rgw-ocs-storagecluster-cephobjectstore.openshift-storage.svc:80"
RGW_STORAGE_CLASS = "ocs-storagecluster-ceph-rgw"

def ensure_obc(api: kubernetes.client.CustomObjectsApi,
               core_api: kubernetes.client.CoreV1Api,
               namespace: str = "troshka-operator") -> dict:
    """Create OBC if it doesn't exist, return S3 config dict."""
    try:
        api.get_namespaced_custom_object(
            "objectbucket.io", "v1alpha1", namespace, "objectbucketclaims", OBC_NAME
        )
    except kubernetes.client.ApiException as e:
        if e.status == 404:
            obc = {
                "apiVersion": "objectbucket.io/v1alpha1",
                "kind": "ObjectBucketClaim",
                "metadata": {"name": OBC_NAME, "namespace": namespace},
                "spec": {
                    "generateBucketName": OBC_NAME,
                    "storageClassName": RGW_STORAGE_CLASS,
                },
            }
            api.create_namespaced_custom_object(
                "objectbucket.io", "v1alpha1", namespace, "objectbucketclaims", obc
            )
        else:
            raise
    return get_obc_s3_config(core_api, namespace)


def get_obc_s3_config(core_api: kubernetes.client.CoreV1Api,
                      namespace: str = "troshka-operator") -> dict | None:
    """Read OBC credentials from auto-generated Secret and ConfigMap."""
    import base64, json
    try:
        secret = core_api.read_namespaced_secret(OBC_NAME, namespace)
        cm = core_api.read_namespaced_config_map(OBC_NAME, namespace)
    except kubernetes.client.ApiException:
        return None

    cm_data = cm.data or {}
    return {
        "bucket": cm_data.get("BUCKET_NAME", ""),
        "endpoint": RGW_ENDPOINT,
        "region": cm_data.get("BUCKET_REGION", "us-east-1") or "us-east-1",
        "access_key_id": base64.b64decode(secret.data["AWS_ACCESS_KEY_ID"]).decode(),
        "secret_access_key": base64.b64decode(secret.data["AWS_SECRET_ACCESS_KEY"]).decode(),
        "credentials_secret": OBC_NAME,
    }
```

- [ ] **Step 2: Wire into operator startup**

In the operator's startup handler (or the first reconcile), call `ensure_obc()` and cache the result. The operator already has startup logic — add the OBC check there.

- [ ] **Step 3: Test OBC helper**

Write a unit test that mocks the K8s API calls and verifies `ensure_obc` creates the OBC when missing and reads credentials when present.

- [ ] **Step 4: Commit**

```bash
git add src/operator/helpers/obc.py src/operator/handlers/project.py tests/
git commit -m "feat: OBC management for local pattern storage"
```

---

## Task 4: Local capture — export Jobs target local RGW

**Files:**
- Modify: `src/operator/helpers/patterns.py` (build_export_job uses OBC config + S3 tuning)
- Modify: `src/operator/handlers/project.py` (_handle_capture passes OBC config)
- Modify: `src/backend/app/services/pattern_service.py` (_capture_kubevirt_native uses OBC config)
- Modify: `src/backend/app/services/s3_storage.py` (add get_cluster_s3_config)

**Interfaces:**
- Consumes: `get_obc_s3_config()` from Task 3
- Modifies: `build_export_job()` — s3_config now comes from OBC, adds `--multipart_chunksize 256MB` and `--max_concurrent_requests 7`

- [ ] **Step 1: Add S3 tuning to build_export_job**

In `src/operator/helpers/patterns.py`, modify the shell script in `build_export_job()` to add before the `aws s3 cp` command:

```bash
aws configure set s3.multipart_chunksize 256MB
aws configure set s3.max_concurrent_requests 7
```

Also add `export HOME=/scratch` at the top of the script (aws configure needs a writable home).

- [ ] **Step 2: Add get_cluster_s3_config to s3_storage.py**

```python
def get_cluster_s3_config(db, provider_id: str) -> dict | None:
    """Get the OBC-based S3 config for a specific KubeVirt cluster.
    Config is stored in the provider's credentials JSON."""
    provider = db.query(Provider).filter_by(id=provider_id).first()
    if not provider or not provider.credentials:
        return None
    creds = provider.credentials if isinstance(provider.credentials, dict) else json.loads(provider.credentials)
    return creds.get("s3_config")
```

- [ ] **Step 3: Modify _capture_kubevirt_native to use OBC config**

In `pattern_service.py`, modify `_capture_kubevirt_native()` to:
1. Get the provider for the host's cluster
2. Build capture_config with OBC S3 config (endpoint = ClusterIP RGW, bucket = OBC bucket)
3. After capture completes, create `PatternLocation` records for the source cluster

- [ ] **Step 4: Modify _handle_capture to pass OBC config**

In `handlers/project.py`, modify `_handle_capture()` to read S3 config from OBC (via `get_obc_s3_config()`) instead of from the capture annotation's s3Config.

- [ ] **Step 5: Write tests**

Test that `build_export_job` includes the S3 tuning commands. Test that the capture config uses the OBC endpoint instead of S4.

- [ ] **Step 6: Commit**

```bash
git commit -m "feat: capture writes to local RGW via OBC instead of S4"
```

---

## Task 5: Pattern location tracking on capture

**Files:**
- Modify: `src/backend/app/services/pattern_service.py` (create locations on capture complete)
- Modify: `src/backend/app/schemas/pattern.py` (add sync status to responses)
- Modify: `src/backend/app/api/patterns.py` (include sync status in list/detail)

**Interfaces:**
- Consumes: `PatternLocation` model from Task 2
- Produces: `PatternLocationResponse` schema, sync status in pattern list/detail responses

- [ ] **Step 1: Create PatternLocation records on capture complete**

In `_finalize_pattern_capture()` (pattern_service.py), after creating PatternDisk records, also create a PatternLocation for each disk on the source cluster:

```python
from app.models.pattern_location import PatternLocation

for disk in pattern.disks:
    loc = PatternLocation(
        pattern_disk_id=disk.id,
        provider_id=pattern.source_provider_id,
        s3_key=disk.s3_key,
        state="synced",
        synced_at=func.now(),
        size_bytes=disk.size_bytes,
    )
    db.add(loc)
```

- [ ] **Step 2: Add sync status to pattern schemas**

```python
# In schemas/pattern.py
class PatternLocationResponse(BaseModel):
    provider_id: str
    provider_name: str | None = None
    state: str
    synced_at: datetime | None = None

class PatternResponse(BaseModel):
    # ... existing fields ...
    source_provider_id: str | None = None
    sync_status: str | None = None  # "local", "syncing", "synced"
    locations: list[PatternLocationResponse] = []
```

- [ ] **Step 3: Compute sync status in API responses**

In `api/patterns.py`, when building responses, query `pattern_locations` to compute aggregate sync status:
- All KubeVirt clusters have all disks → "synced"
- Some clusters missing → "syncing N/M" or "local"
- No locations → "local" (legacy S4 pattern)

- [ ] **Step 4: Write tests and commit**

---

## Task 6: Deploy path — use per-cluster RGW endpoint

**Files:**
- Modify: `src/operator/helpers/kubevirt.py` (build_datavolume_from_s3 uses cluster-specific endpoint)
- Modify: `src/operator/handlers/project.py` (_precreate_golden_pvcs uses local RGW)

**Interfaces:**
- Consumes: OBC S3 config from Task 3
- Modifies: `build_datavolume_from_s3()` — endpoint comes from OBC config, not global S3 config

- [ ] **Step 1: Modify golden PVC creation to use local RGW**

In `_precreate_golden_pvcs()`, when constructing S3 config for CDI DataVolumes:
1. Check if the pattern disk has a `PatternLocation` for the current cluster
2. If yes, use the local RGW endpoint + OBC credentials
3. If no, fall back to S4 (migration compatibility)

- [ ] **Step 2: Update _ensure_cache_namespace_and_secrets**

The S3 credentials Secret in `troshka-cache` namespace needs the OBC credentials (not the S4 credentials) when deploying from local RGW.

- [ ] **Step 3: Write tests and commit**

---

## Task 7: Sync Job handler in operator

**Files:**
- Create: `src/operator/handlers/sync.py`
- Modify: `src/operator/handlers/project.py` (register sync annotation handler)

**Interfaces:**
- Consumes: OBC config from Task 3, rclone from Task 1
- Produces: Sync Job that copies pattern disk from source cluster's RGW (Route) to local RGW (ClusterIP) using rclone

- [ ] **Step 1: Create sync handler**

The handler watches for a `troshka.redhat.com/sync-request` annotation on a ConfigMap or dedicated CR. It:
1. Parses the sync request (source endpoint, credentials secret, s3_key, size)
2. Creates a K8s Job using troshka-tools image
3. Job runs rclone with config written to `/tmp/rclone.conf`:

```bash
export RCLONE_CONFIG=/tmp/rclone.conf
cat > $RCLONE_CONFIG <<EOF
[source]
type = s3
provider = Ceph
access_key_id = $SOURCE_ACCESS_KEY
secret_access_key = $SOURCE_SECRET_KEY
endpoint = $SOURCE_ENDPOINT
no_check_bucket = true
no_verify_ssl = true

[target]
type = s3
provider = Ceph
access_key_id = $LOCAL_ACCESS_KEY
secret_access_key = $LOCAL_SECRET_KEY
endpoint = http://rook-ceph-rgw-ocs-storagecluster-cephobjectstore.openshift-storage.svc:80
no_check_bucket = true
EOF

rclone copyto "source:$SOURCE_BUCKET/$S3_KEY" "target:$LOCAL_BUCKET/$S3_KEY" \
  --s3-chunk-size 256M --s3-upload-concurrency 7 --transfers 1
```

4. Reports progress via Job status / CR annotation
5. Job resources: 4 GiB memory request, 8 GiB limit (rclone buffers ~1.8 GiB for 7 × 256MB chunks)

- [ ] **Step 2: Write tests and commit**

---

## Task 8: Sync orchestration in backend

**Files:**
- Create: `src/backend/app/services/pattern_sync_service.py`
- Create: `src/backend/app/api/pattern_sync.py`
- Modify: `src/backend/app/main.py` (register router)

**Interfaces:**
- Consumes: `PatternLocation` from Task 2, operator sync handler from Task 7
- Produces: `sync_pattern_to_cluster(db, pattern_id, target_provider_id)`, `sync_pattern_to_all(db, pattern_id)`, `publish_pattern(db, pattern_id)`

- [ ] **Step 1: Create sync service**

```python
# src/backend/app/services/pattern_sync_service.py

def sync_pattern_to_cluster(db, pattern_id: str, target_provider_id: str):
    """Trigger sync of all pattern disks to a target cluster."""
    # 1. Get pattern + disks
    # 2. Get source cluster's OBC credentials (from source_provider_id)
    # 3. Get target cluster's K8s clients
    # 4. Ensure source credentials Secret exists on target cluster
    # 5. For each disk without a synced PatternLocation on target:
    #    - Create PatternLocation(state="syncing")
    #    - Patch operator CR/annotation with sync request
    # 6. Enqueue background job to poll sync status

def sync_pattern_to_all(db, pattern_id: str):
    """Sync pattern to all KubeVirt clusters."""
    # Get all kubevirt providers, fan out sync_pattern_to_cluster for each

def publish_pattern(db, pattern_id: str):
    """Set pattern visibility to public and trigger sync to all clusters."""
    # Update visibility, then call sync_pattern_to_all
```

- [ ] **Step 2: Create sync API endpoints**

```python
# src/backend/app/api/pattern_sync.py
router = APIRouter(prefix="/patterns", tags=["patterns"])

@router.post("/{pattern_id}/sync")
# Manual sync to a specific cluster or all clusters

@router.post("/{pattern_id}/publish")
# Admin-only: set public + auto-sync

@router.get("/{pattern_id}/sync-status")
# Per-cluster sync status
```

- [ ] **Step 3: Credential distribution**

When triggering a sync, the backend creates/updates a `sync-source-{cluster_id}` Secret on the target cluster with the source cluster's OBC credentials. This is analogous to how `_ensure_s3_secret` works for capture.

- [ ] **Step 4: Write tests and commit**

---

## Task 9: Frontend — sync status + publish

**Files:**
- Modify: `src/frontend/src/app/library/patterns/page.tsx`

**Interfaces:**
- Consumes: Sync status from pattern API responses (Task 5), sync/publish endpoints (Task 8)

- [ ] **Step 1: Add sync status badge to pattern cards**

In the patterns list, add a badge next to each pattern showing:
- "Local" (orange) — `sync_status === "local"`
- "Syncing 3/9" (yellow) — `sync_status` starts with "syncing"
- "Available" (green) — `sync_status === "synced"`

- [ ] **Step 2: Add publish button (admin only)**

In the pattern card actions dropdown, add "Publish" option that calls `POST /patterns/{id}/publish`. Only shown for admin users.

- [ ] **Step 3: Add manual sync in pattern detail**

When viewing a pattern's detail, show per-cluster sync status and a "Sync" button for clusters that don't have the pattern yet.

- [ ] **Step 4: Test in browser and commit**

---

## Task 10: S4 migration

**Files:**
- Create: `scripts/migrate-s4-to-rgw.py`

**Interfaces:**
- Consumes: Existing S4 patterns, PatternLocation from Task 2, sync service from Task 8

- [ ] **Step 1: Write migration script**

Script that:
1. Lists all patterns and their disks from the DB
2. For patterns with no `pattern_locations` entries:
   a. Determines a "home" cluster (from source_project's host, or a default)
   b. Triggers a sync from S4 to the home cluster's RGW
   c. Creates PatternLocation records
3. For public/gold patterns, syncs to all clusters
4. Reports progress and handles errors

- [ ] **Step 2: Test with a single pattern and commit**

---

## Task 11: Visibility and auto-sync

**Files:**
- Modify: `src/backend/app/services/pattern_sync_service.py` (auto-sync on publish)
- Modify: `src/backend/app/api/patterns.py` (visibility update triggers sync)

**Interfaces:**
- Consumes: sync_pattern_to_all from Task 8

- [ ] **Step 1: Auto-sync on visibility change to public**

When a pattern's visibility changes to `public` (via PATCH /patterns/{id}), automatically trigger `sync_pattern_to_all()`.

- [ ] **Step 2: On-demand sync on deploy**

When deploying a pattern to a cluster that doesn't have it:
1. Check `pattern_locations` for target cluster
2. If missing, trigger sync and return "syncing" status
3. Frontend polls until sync complete, then deploys

- [ ] **Step 3: Write tests and commit**

---

## Implementation Phases

| Phase | Tasks | Deliverable |
|-------|-------|-------------|
| **Phase 1: Local capture** | 1, 2, 3, 4, 5, 6 | Capture writes to local RGW, deploy from local RGW. S4 still works as fallback. |
| **Phase 2: Cross-cluster sync** | 7, 8 | Background sync between clusters via rclone. |
| **Phase 3: Frontend + visibility** | 9, 11 | Sync status UI, publish button, auto-sync on publish. |
| **Phase 4: Migration** | 10 | Migrate existing S4 patterns, decommission S4. |

Each phase produces working, deployable software. Phase 1 is the critical path — it delivers the 8-16x capture speedup immediately.
