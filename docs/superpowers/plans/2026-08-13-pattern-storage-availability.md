# Pattern Storage Availability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a Pattern-Capture-v2 pattern deployable on its source cluster immediately and on any other cluster once synced to central S4, with placement and a deploy pre-flight that refuse to launch CDI when the pattern's storage is not fully available.

**Architecture:** `PatternLocation` becomes the single source of truth for where each pattern disk lives (`obc` on a specific cluster, or `central` in S4 `troshka-images`). A readiness predicate built on it is reused by placement (filter to storage-ready clusters) and deploy (per-disk source selection + pre-flight verification). Capture eagerly enqueues an RQ job that orchestrates an rclone Job on the source cluster to copy disks OBC→central. The operator routes each pattern disk to the OBC or central endpoint based on a per-disk `source` carried in the CR topology.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.0, Alembic, Dynaconf, RQ (Redis), boto3, Kubernetes Python client (BatchV1Api), kopf operator. Tests: pytest on SQLite.

**Spec:** `docs/superpowers/specs/2026-08-13-pattern-storage-availability-design.md`

## Global Constraints

- Terminology (do not confuse): **central S4** = primary `s3_config` = `_get_s3_config()` = bucket `troshka-images`. **gold / central-library (OUT OF SCOPE)** = `_get_readonly_s3_config()` = `troshka-gold-images` (`type='s3_readonly'`). **local RGW / OBC** = `get_cluster_s3_config(db, provider_id)` = per-cluster Ceph OBC bucket.
- "All disks or not ready" — a pattern is deployable on a cluster only if **every** pattern disk resolves to a source there. No partial deploys.
- FK columns in migrations use `postgresql.UUID(as_uuid=False)` (never `String(36)`).
- The OBC endpoint is an in-cluster `.svc` address **unreachable from the infra01 backend/worker pods**. Backend code must never attempt a live S3 call (head_object/get_object) against an OBC endpoint. For `obc`-source disks, the `synced` PatternLocation row is authoritative. Live HEAD pre-flight applies to `central`-source disks only.
- SQLAlchemy 2.0 style: `Mapped[type]` + `mapped_column()`. UUID string PKs: `default=lambda: str(uuid.uuid4())`.
- Cognitive complexity ≤ 15 per function (SonarQube S3776) — extract helpers rather than nesting.
- Run `black` (system, not venv) before every commit. Do NOT add `Co-Authored-By` lines. Never amend; always a new commit. Use `python3`. Git from project root with absolute paths.
- Tests run on Python 3.13; add extra trailing values to any `time.time()` mocks to avoid `StopIteration`.
- Work happens on branch `pattern-storage-availability` (already checked out). Current alembic head: `33827935d7e4`.
- Backend has no auto-reload; after Python changes the user restarts the backend manually — never restart it yourself, and remind the user when a change needs a restart to take effect.

---

### Task 1: PatternLocation gains `location_type` + nullable `provider_id`

Introduces the data-model change everything else builds on: a `location_type` column (`"obc"` | `"central"`) and a nullable `provider_id` (NULL for central rows).

**Files:**
- Modify: `src/backend/app/models/pattern_location.py:29-33` (make `provider_id` nullable), add `location_type` column after line 34.
- Create: `src/backend/alembic/versions/<generated>_add_location_type_to_pattern_locations.py`
- Test: `src/backend/tests/test_pattern_location_model.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `PatternLocation.location_type: Mapped[str]` (default `"obc"`), `PatternLocation.provider_id: Mapped[str | None]` (nullable). Column string values used everywhere: `"obc"`, `"central"`. State string values: `"syncing"`, `"synced"`, `"error"`.

- [ ] **Step 1: Write the failing test**

Create `src/backend/tests/test_pattern_location_model.py`:

```python
import uuid

from app.models.pattern import Pattern, PatternDisk
from app.models.pattern_location import PatternLocation
from app.models.user import User


def _make_pattern_disk(db):
    user = User(id=str(uuid.uuid4()), email="t@example.com", name="t")
    db.add(user)
    db.flush()
    pattern = Pattern(
        id=str(uuid.uuid4()), name="p", owner_id=user.id, topology={"nodes": []}
    )
    db.add(pattern)
    db.flush()
    pd = PatternDisk(
        id=str(uuid.uuid4()),
        pattern_id=pattern.id,
        source_disk_id="d",
        source_vm_id="v",
        s3_key="patterns/x/d.qcow2",
        format="qcow2",
        state="available",
    )
    db.add(pd)
    db.flush()
    return pd


def test_obc_location_defaults(db_session):
    pd = _make_pattern_disk(db_session)
    loc = PatternLocation(
        pattern_disk_id=pd.id,
        provider_id=str(uuid.uuid4()),
        s3_key=pd.s3_key,
        state="synced",
    )
    db_session.add(loc)
    db_session.flush()
    assert loc.location_type == "obc"


def test_central_location_has_null_provider(db_session):
    pd = _make_pattern_disk(db_session)
    loc = PatternLocation(
        pattern_disk_id=pd.id,
        provider_id=None,
        location_type="central",
        s3_key=pd.s3_key,
        state="synced",
    )
    db_session.add(loc)
    db_session.flush()
    assert loc.provider_id is None
    assert loc.location_type == "central"
```

> The `db_session` fixture is the existing SQLite session fixture used across `src/backend/tests/`. If a test needs a differently named fixture, check `src/backend/tests/conftest.py` and match the existing project fixture name.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_pattern_location_model.py -v`
Expected: FAIL — `test_central_location_has_null_provider` raises an IntegrityError / cannot set `provider_id=None`, and `location_type` attribute does not exist.

- [ ] **Step 3: Modify the model**

In `src/backend/app/models/pattern_location.py`, make `provider_id` nullable and add `location_type`:

```python
    provider_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("providers.id", ondelete="CASCADE"),
        nullable=True,
    )
    location_type: Mapped[str] = mapped_column(
        String(20), default="obc", server_default="obc", nullable=False
    )
    s3_key: Mapped[str] = mapped_column(String(500), nullable=False)
```

Also relax the `provider` relationship so a NULL provider is allowed (leave the `relationship()` as-is — it already tolerates NULL FKs).

- [ ] **Step 4: Generate the migration**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m alembic revision -m "add location_type to pattern_locations"`

Edit the generated file so `upgrade()` / `downgrade()` read exactly:

```python
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


def upgrade():
    op.add_column(
        "pattern_locations",
        sa.Column(
            "location_type",
            sa.String(length=20),
            nullable=False,
            server_default="obc",
        ),
    )
    op.alter_column(
        "pattern_locations",
        "provider_id",
        existing_type=postgresql.UUID(as_uuid=False),
        nullable=True,
    )


def downgrade():
    op.alter_column(
        "pattern_locations",
        "provider_id",
        existing_type=postgresql.UUID(as_uuid=False),
        nullable=False,
    )
    op.drop_column("pattern_locations", "location_type")
```

Confirm the generated `down_revision = "33827935d7e4"`.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_pattern_location_model.py -v`
Expected: PASS (SQLite tables are created from the models directly; the migration is exercised in prod/Postgres).

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka
black src/backend/app/models/pattern_location.py src/backend/tests/test_pattern_location_model.py
git add src/backend/app/models/pattern_location.py src/backend/alembic/versions/ src/backend/tests/test_pattern_location_model.py
git commit -m "feat(patterns): add location_type and nullable provider_id to PatternLocation"
```

---

### Task 2: Readiness predicate & source-selection helpers

Pure, DB-only helpers that decide where a pattern disk can be sourced from on a given cluster. Reused by deploy (Task 5) and placement (Task 7).

**Files:**
- Create: `src/backend/app/services/pattern_locations.py`
- Test: `src/backend/tests/test_pattern_locations.py`

**Interfaces:**
- Consumes: `PatternLocation` (Task 1), `PatternLocation.location_type`, `PatternLocation.state`, `PatternLocation.provider_id`.
- Produces:
  - `pattern_disk_ids_from_topology(topology: dict) -> list[str]` — patternDiskId for every storageNode whose `data.source == "pattern"`.
  - `pattern_disk_source_for_cluster(db, pattern_disk_id: str, target_provider_id: str | None) -> str | None` — returns `"obc"` if a synced obc location on `target_provider_id` exists, else `"central"` if a synced central location exists, else `None`.
  - `pattern_disks_ready_on_provider(db, pattern_disk_ids: list[str], target_provider_id: str | None) -> bool` — True iff every id resolves to a non-None source. Empty list → True.

- [ ] **Step 1: Write the failing test**

Create `src/backend/tests/test_pattern_locations.py`:

```python
import uuid

from app.models.pattern import Pattern, PatternDisk
from app.models.pattern_location import PatternLocation
from app.models.user import User
from app.services.pattern_locations import (
    pattern_disk_ids_from_topology,
    pattern_disk_source_for_cluster,
    pattern_disks_ready_on_provider,
)


def _disk(db):
    user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4()}@e.com", name="t")
    db.add(user)
    db.flush()
    pat = Pattern(id=str(uuid.uuid4()), name="p", owner_id=user.id, topology={})
    db.add(pat)
    db.flush()
    pd = PatternDisk(
        id=str(uuid.uuid4()),
        pattern_id=pat.id,
        source_disk_id="d",
        source_vm_id="v",
        s3_key="patterns/x/d.qcow2",
        format="qcow2",
        state="available",
    )
    db.add(pd)
    db.flush()
    return pd


PROV_A = str(uuid.uuid4())
PROV_B = str(uuid.uuid4())


def test_ids_from_topology():
    topo = {
        "nodes": [
            {"type": "storageNode", "data": {"source": "pattern", "patternDiskId": "d1"}},
            {"type": "storageNode", "data": {"source": "library", "libraryItemId": "l1"}},
            {"type": "vmNode", "data": {}},
        ]
    }
    assert pattern_disk_ids_from_topology(topo) == ["d1"]


def test_obc_on_target_provider(db_session):
    pd = _disk(db_session)
    db_session.add(
        PatternLocation(
            pattern_disk_id=pd.id, provider_id=PROV_A, location_type="obc",
            s3_key=pd.s3_key, state="synced",
        )
    )
    db_session.flush()
    assert pattern_disk_source_for_cluster(db_session, pd.id, PROV_A) == "obc"
    # OBC on A is not visible to B, and no central row → None on B
    assert pattern_disk_source_for_cluster(db_session, pd.id, PROV_B) is None


def test_central_visible_everywhere(db_session):
    pd = _disk(db_session)
    db_session.add(
        PatternLocation(
            pattern_disk_id=pd.id, provider_id=None, location_type="central",
            s3_key=pd.s3_key, state="synced",
        )
    )
    db_session.flush()
    assert pattern_disk_source_for_cluster(db_session, pd.id, PROV_A) == "central"
    assert pattern_disk_source_for_cluster(db_session, pd.id, PROV_B) == "central"


def test_obc_preferred_over_central_on_source(db_session):
    pd = _disk(db_session)
    db_session.add_all(
        [
            PatternLocation(
                pattern_disk_id=pd.id, provider_id=PROV_A, location_type="obc",
                s3_key=pd.s3_key, state="synced",
            ),
            PatternLocation(
                pattern_disk_id=pd.id, provider_id=None, location_type="central",
                s3_key=pd.s3_key, state="synced",
            ),
        ]
    )
    db_session.flush()
    assert pattern_disk_source_for_cluster(db_session, pd.id, PROV_A) == "obc"


def test_syncing_central_is_not_ready(db_session):
    pd = _disk(db_session)
    db_session.add(
        PatternLocation(
            pattern_disk_id=pd.id, provider_id=None, location_type="central",
            s3_key=pd.s3_key, state="syncing",
        )
    )
    db_session.flush()
    assert pattern_disk_source_for_cluster(db_session, pd.id, PROV_A) is None


def test_ready_requires_all_disks(db_session):
    pd1 = _disk(db_session)
    pd2 = _disk(db_session)
    db_session.add(
        PatternLocation(
            pattern_disk_id=pd1.id, provider_id=None, location_type="central",
            s3_key=pd1.s3_key, state="synced",
        )
    )
    db_session.flush()
    assert pattern_disks_ready_on_provider(db_session, [pd1.id], PROV_A) is True
    assert pattern_disks_ready_on_provider(db_session, [pd1.id, pd2.id], PROV_A) is False
    assert pattern_disks_ready_on_provider(db_session, [], PROV_A) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_pattern_locations.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.pattern_locations`.

- [ ] **Step 3: Write the implementation**

Create `src/backend/app/services/pattern_locations.py`:

```python
"""Pattern disk location predicates.

Single source of truth for deciding where each pattern disk can be sourced
from on a given cluster: the cluster's local RGW/OBC, central S4
(`troshka-images`), or neither. Reused by placement and deploy so both apply
identical "all disks or not ready" logic.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.pattern_location import PatternLocation


def pattern_disk_ids_from_topology(topology: dict) -> list[str]:
    """Return patternDiskId for every pattern-sourced storageNode in a topology."""
    ids: list[str] = []
    for node in (topology or {}).get("nodes", []):
        if node.get("type") != "storageNode":
            continue
        data = node.get("data", {})
        if data.get("source") == "pattern" and data.get("patternDiskId"):
            ids.append(data["patternDiskId"])
    return ids


def pattern_disk_source_for_cluster(
    db: Session, pattern_disk_id: str, target_provider_id: str | None
) -> str | None:
    """Where can this disk be sourced from on target_provider_id?

    Returns "obc" (local RGW on that provider), "central" (S4 troshka-images),
    or None if the disk is not synced anywhere reachable from that cluster.
    OBC on the target provider is preferred over central.
    """
    if target_provider_id:
        obc = (
            db.query(PatternLocation)
            .filter_by(
                pattern_disk_id=pattern_disk_id,
                provider_id=target_provider_id,
                location_type="obc",
                state="synced",
            )
            .first()
        )
        if obc:
            return "obc"
    central = (
        db.query(PatternLocation)
        .filter_by(
            pattern_disk_id=pattern_disk_id,
            location_type="central",
            state="synced",
        )
        .first()
    )
    if central:
        return "central"
    return None


def pattern_disks_ready_on_provider(
    db: Session, pattern_disk_ids: list[str], target_provider_id: str | None
) -> bool:
    """True iff every disk resolves to a non-None source on target_provider_id."""
    if not pattern_disk_ids:
        return True
    return all(
        pattern_disk_source_for_cluster(db, did, target_provider_id) is not None
        for did in pattern_disk_ids
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_pattern_locations.py -v`
Expected: PASS (all 6 tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka
black src/backend/app/services/pattern_locations.py src/backend/tests/test_pattern_locations.py
git add src/backend/app/services/pattern_locations.py src/backend/tests/test_pattern_locations.py
git commit -m "feat(patterns): add readiness predicate and source-selection helpers"
```

---

### Task 3: OBC→central sync worker with capacity guard

An RQ job that copies a pattern's disks from the source cluster's OBC to central S4 via an rclone Job on the source cluster, guarded by a configured central capacity ceiling.

**Files:**
- Create: `src/backend/app/services/pattern_sync.py`
- Modify: `src/backend/config/config.yaml` (add `central_s4.max_bytes`)
- Test: `src/backend/tests/test_pattern_sync.py`

**Interfaces:**
- Consumes: `PatternLocation` (Task 1); `get_cluster_s3_config(db, provider_id)` and `_get_s3_config()` from `app.services.s3_storage`; `_get_k8s_clients(provider)` from `app.services.providers.kubevirt`; `Pattern`, `PatternDisk` models.
- Produces:
  - `central_capacity_available(db, additional_bytes: int) -> bool` — sum of synced central `size_bytes` + `additional_bytes` ≤ configured `central_s4.max_bytes` (True when unconfigured/None).
  - `sync_pattern_to_central(pattern_id: str) -> None` — the RQ worker entrypoint.
  - `build_sync_rclone_job(name, namespace, keys, src_cfg, dst_cfg) -> dict` — the BatchV1 Job body (pure; unit-tested without a cluster).

- [ ] **Step 1: Write the failing test**

Create `src/backend/tests/test_pattern_sync.py`:

```python
import uuid
from unittest.mock import patch

from app.models.pattern import Pattern, PatternDisk
from app.models.pattern_location import PatternLocation
from app.models.user import User
from app.services import pattern_sync


def _pattern_with_disks(db, provider_id, total_bytes=1000):
    user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4()}@e.com", name="t")
    db.add(user)
    db.flush()
    pat = Pattern(
        id=str(uuid.uuid4()),
        name="p",
        owner_id=user.id,
        topology={},
        source_provider_id=provider_id,
        total_size_bytes=total_bytes,
    )
    db.add(pat)
    db.flush()
    pd = PatternDisk(
        id=str(uuid.uuid4()),
        pattern_id=pat.id,
        source_disk_id="d",
        source_vm_id="v",
        s3_key="patterns/x/d.qcow2",
        format="qcow2",
        size_bytes=total_bytes,
        state="available",
    )
    db.add(pd)
    db.add(
        PatternLocation(
            pattern_disk_id=pd.id, provider_id=provider_id, location_type="obc",
            s3_key=pd.s3_key, state="synced", size_bytes=total_bytes,
        )
    )
    db.flush()
    return pat, pd


def test_capacity_available_when_unconfigured(db_session):
    with patch.object(pattern_sync, "_max_central_bytes", return_value=None):
        assert pattern_sync.central_capacity_available(db_session, 10**12) is True


def test_capacity_guard_blocks_over_ceiling(db_session):
    pat, pd = _pattern_with_disks(db_session, str(uuid.uuid4()), total_bytes=100)
    db_session.add(
        PatternLocation(
            pattern_disk_id=pd.id, provider_id=None, location_type="central",
            s3_key=pd.s3_key, state="synced", size_bytes=900,
        )
    )
    db_session.flush()
    with patch.object(pattern_sync, "_max_central_bytes", return_value=1000):
        assert pattern_sync.central_capacity_available(db_session, 50) is False
        assert pattern_sync.central_capacity_available(db_session, 100) is True


def test_rclone_job_body_copies_all_keys():
    body = pattern_sync.build_sync_rclone_job(
        "sync-abc",
        "troshka-cache",
        ["patterns/x/a.qcow2", "patterns/x/b.qcow2"],
        {"access_key_id": "AK", "secret_access_key": "SK",
         "endpoint": "https://rgw.svc", "bucket": "obc-bucket"},
        {"access_key_id": "CK", "secret_access_key": "CS",
         "endpoint_url": "https://s4", "bucket": "troshka-images"},
    )
    assert body["kind"] == "Job"
    cmd = body["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    assert "patterns/x/a.qcow2" in cmd
    assert "patterns/x/b.qcow2" in cmd
    assert "obc-bucket" in cmd
    assert "troshka-images" in cmd


def test_sync_marks_synced_on_job_success(db_session):
    provider_id = str(uuid.uuid4())
    pat, pd = _pattern_with_disks(db_session, provider_id, total_bytes=500)

    with patch.object(pattern_sync, "SessionLocal", return_value=db_session), \
         patch.object(pattern_sync, "_max_central_bytes", return_value=None), \
         patch.object(pattern_sync, "get_cluster_s3_config",
                      return_value={"access_key_id": "AK", "secret_access_key": "SK",
                                    "endpoint": "https://rgw.svc", "bucket": "obc"}), \
         patch.object(pattern_sync, "_get_s3_config",
                      return_value={"access_key_id": "CK", "secret_access_key": "CS",
                                    "endpoint_url": "https://s4", "bucket": "troshka-images",
                                    "region": "us-east-1"}), \
         patch.object(pattern_sync, "_run_rclone_job", return_value=True):
        pattern_sync.sync_pattern_to_central(pat.id)

    central = (
        db_session.query(PatternLocation)
        .filter_by(pattern_disk_id=pd.id, location_type="central")
        .first()
    )
    assert central is not None
    assert central.state == "synced"
    assert central.provider_id is None


def test_sync_capacity_rejection_marks_error(db_session):
    provider_id = str(uuid.uuid4())
    pat, pd = _pattern_with_disks(db_session, provider_id, total_bytes=500)

    with patch.object(pattern_sync, "SessionLocal", return_value=db_session), \
         patch.object(pattern_sync, "_max_central_bytes", return_value=100), \
         patch.object(pattern_sync, "_run_rclone_job") as run:
        pattern_sync.sync_pattern_to_central(pat.id)
        run.assert_not_called()

    central = (
        db_session.query(PatternLocation)
        .filter_by(pattern_disk_id=pd.id, location_type="central")
        .first()
    )
    assert central is not None
    assert central.state == "error"
    assert "capacity" in (central.error_message or "").lower()
```

> `SessionLocal` is patched to return the test's `db_session` so the worker's own session is the test session. The worker calls `db.close()` at the end guarded so the patched session survives the assertions (see implementation — it uses the returned session directly and only closes a session it opened; patching makes `close()` a harmless no-op on the shared session, which SQLite tolerates within the test).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_pattern_sync.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.pattern_sync`.

- [ ] **Step 3: Add the config key**

In `src/backend/config/config.yaml`, add a top-level block (place near other storage/s3 settings):

```yaml
central_s4:
  # Soft ceiling for central S4 (troshka-images). Sync jobs refuse to copy a
  # pattern up when synced central bytes + the pattern size would exceed this.
  # Null / omitted = no ceiling (guard disabled).
  max_bytes: null
```

- [ ] **Step 4: Write the implementation**

Create `src/backend/app/services/pattern_sync.py`:

```python
"""Eager OBC -> central S4 sync for captured patterns.

An RQ worker orchestrates the copy; the bytes move via an rclone Job on the
source cluster (the OBC endpoint is an in-cluster .svc address unreachable
from the backend workers). Central S4 = the primary `troshka-images` bucket.
"""

from __future__ import annotations

import logging
import time

from app.core.database import SessionLocal
from app.models.pattern import Pattern, PatternDisk
from app.models.pattern_location import PatternLocation
from app.models.provider import Provider
from app.services.s3_storage import _get_s3_config, get_cluster_s3_config

log = logging.getLogger(__name__)

SYNC_NAMESPACE = "troshka-cache"
_SYNC_POLL_TIMEOUT = 3600
_SYNC_POLL_INTERVAL = 10


def _max_central_bytes() -> int | None:
    """Configured central S4 ceiling in bytes, or None when unset."""
    from app.core.config import config

    return getattr(getattr(config, "central_s4", None), "max_bytes", None)


def _synced_central_bytes(db) -> int:
    total = 0
    rows = (
        db.query(PatternLocation)
        .filter_by(location_type="central", state="synced")
        .all()
    )
    for r in rows:
        total += r.size_bytes or 0
    return total


def central_capacity_available(db, additional_bytes: int) -> bool:
    """True if additional_bytes fit under the configured central ceiling."""
    ceiling = _max_central_bytes()
    if ceiling is None:
        return True
    return _synced_central_bytes(db) + additional_bytes <= ceiling


def build_sync_rclone_job(name, namespace, keys, src_cfg, dst_cfg) -> dict:
    """Build a BatchV1 Job that rclone-copies each key OBC(src) -> central(dst)."""
    src_endpoint = src_cfg.get("endpoint", "") or src_cfg.get("endpoint_url", "")
    dst_endpoint = dst_cfg.get("endpoint_url", "") or dst_cfg.get("endpoint", "")
    src_bucket = src_cfg.get("bucket", "")
    dst_bucket = dst_cfg.get("bucket", "troshka-images")

    copies = "\n".join(
        f'rclone copyto "src:{src_bucket}/{k}" "dst:{dst_bucket}/{k}" '
        f"--s3-chunk-size 64M --s3-upload-concurrency 4 "
        f"--log-level INFO --stats 15s --stats-one-line;"
        for k in keys
    )
    cmd = (
        "set -e; export HOME=/tmp; export RCLONE_CONFIG=/tmp/rclone.conf;\n"
        "cat > $RCLONE_CONFIG <<REOF\n"
        "[src]\n"
        "type = s3\n"
        "provider = Ceph\n"
        "access_key_id = $SRC_ACCESS_KEY_ID\n"
        "secret_access_key = $SRC_SECRET_ACCESS_KEY\n"
        f"endpoint = {src_endpoint}\n"
        "no_check_bucket = true\n"
        "no_verify_ssl = true\n"
        "[dst]\n"
        "type = s3\n"
        "provider = Ceph\n"
        "access_key_id = $DST_ACCESS_KEY_ID\n"
        "secret_access_key = $DST_SECRET_ACCESS_KEY\n"
        f"endpoint = {dst_endpoint}\n"
        "no_check_bucket = true\n"
        "no_verify_ssl = true\n"
        "REOF\n" + copies
    )
    env = [
        {"name": "SRC_ACCESS_KEY_ID", "value": src_cfg.get("access_key_id", "")},
        {"name": "SRC_SECRET_ACCESS_KEY", "value": src_cfg.get("secret_access_key", "")},
        {"name": "DST_ACCESS_KEY_ID", "value": dst_cfg.get("access_key_id", "")},
        {"name": "DST_SECRET_ACCESS_KEY", "value": dst_cfg.get("secret_access_key", "")},
    ]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"troshka-role": "pattern-sync"},
        },
        "spec": {
            "backoffLimit": 3,
            "activeDeadlineSeconds": _SYNC_POLL_TIMEOUT,
            "ttlSecondsAfterFinished": 600,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "sync",
                            "image": "rclone/rclone:latest",
                            "command": ["sh", "-c", cmd],
                            "env": env,
                            "resources": {
                                "requests": {"cpu": "500m", "memory": "512Mi"},
                                "limits": {"cpu": "2", "memory": "2Gi"},
                            },
                        }
                    ],
                }
            },
        },
    }


def _run_rclone_job(provider, name, keys, src_cfg, dst_cfg) -> bool:
    """Create the rclone Job on the source cluster and poll to completion."""
    from kubernetes import client as k8s_client

    from app.services.providers.kubevirt import _get_k8s_clients

    _custom, core_api, api_client = _get_k8s_clients(provider)
    batch_api = k8s_client.BatchV1Api(api_client)

    try:
        core_api.create_namespace(
            body=k8s_client.V1Namespace(
                metadata=k8s_client.V1ObjectMeta(name=SYNC_NAMESPACE)
            )
        )
    except Exception as e:
        if "AlreadyExists" not in str(e):
            raise

    body = build_sync_rclone_job(name, SYNC_NAMESPACE, keys, src_cfg, dst_cfg)
    try:
        batch_api.create_namespaced_job(namespace=SYNC_NAMESPACE, body=body)
    except Exception as e:
        if "AlreadyExists" not in str(e):
            raise

    waited = 0
    while waited < _SYNC_POLL_TIMEOUT:
        job = batch_api.read_namespaced_job(name=name, namespace=SYNC_NAMESPACE)
        status = job.status
        if status and status.succeeded:
            return True
        if status and status.failed:
            return False
        time.sleep(_SYNC_POLL_INTERVAL)
        waited += _SYNC_POLL_INTERVAL
    return False


def _fail_central_rows(db, rows, message):
    for r in rows:
        r.state = "error"
        r.error_message = message[:500]
    db.commit()


def sync_pattern_to_central(pattern_id: str) -> None:
    """RQ worker: copy a pattern's disks from source OBC to central S4."""
    db = SessionLocal()
    try:
        pattern = db.query(Pattern).filter_by(id=pattern_id).first()
        if not pattern or not pattern.source_provider_id:
            log.warning("sync: pattern %s missing or has no source provider", pattern_id)
            return
        disks = db.query(PatternDisk).filter_by(pattern_id=pattern_id).all()

        # Create/collect central rows for disks not already synced centrally.
        pending = []
        for pd in disks:
            existing = (
                db.query(PatternLocation)
                .filter_by(pattern_disk_id=pd.id, location_type="central")
                .first()
            )
            if existing and existing.state == "synced":
                continue
            if not existing:
                existing = PatternLocation(
                    pattern_disk_id=pd.id,
                    provider_id=None,
                    location_type="central",
                    s3_key=pd.s3_key,
                    state="syncing",
                    size_bytes=pd.size_bytes or 0,
                )
                db.add(existing)
            else:
                existing.state = "syncing"
                existing.error_message = None
            pending.append((pd, existing))
        if not pending:
            log.info("sync: pattern %s already central; nothing to do", pattern_id)
            return
        db.commit()

        addl = sum((pd.size_bytes or 0) for pd, _ in pending)
        if not central_capacity_available(db, addl):
            _fail_central_rows(
                db, [row for _, row in pending],
                "central S4 capacity exceeded — cannot sync pattern",
            )
            log.error("sync: capacity guard rejected pattern %s (%d bytes)", pattern_id, addl)
            return

        provider = db.query(Provider).filter_by(id=pattern.source_provider_id).first()
        src_cfg = get_cluster_s3_config(db, pattern.source_provider_id)
        dst_cfg = _get_s3_config()
        if not provider or not src_cfg:
            _fail_central_rows(
                db, [row for _, row in pending],
                "source cluster OBC config unavailable",
            )
            return

        keys = [pd.s3_key for pd, _ in pending]
        job_name = f"sync-{pattern_id[:8]}"
        ok = _run_rclone_job(provider, job_name, keys, src_cfg, dst_cfg)
        if ok:
            import datetime

            now = datetime.datetime.now(datetime.UTC)
            for _pd, row in pending:
                row.state = "synced"
                row.synced_at = now
            db.commit()
            log.info("sync: pattern %s synced %d disks to central", pattern_id, len(keys))
        else:
            _fail_central_rows(
                db, [row for _, row in pending],
                "rclone sync job failed or timed out",
            )
    finally:
        db.close()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_pattern_sync.py -v`
Expected: PASS (all 5 tests).

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka
black src/backend/app/services/pattern_sync.py src/backend/tests/test_pattern_sync.py
git add src/backend/app/services/pattern_sync.py src/backend/config/config.yaml src/backend/tests/test_pattern_sync.py
git commit -m "feat(patterns): add OBC->central sync worker with capacity guard"
```

---

### Task 4: Enqueue sync eagerly on capture completion

Wire the sync worker into the end of a successful KubeVirt capture so a captured pattern begins replicating to central S4 immediately.

**Files:**
- Modify: `src/backend/app/services/pattern_service.py:829-834` (after `pattern.state = "available"` / commit / notify).
- Test: `src/backend/tests/test_pattern_capture_sync_enqueue.py`

**Interfaces:**
- Consumes: `sync_pattern_to_central` (Task 3); `enqueue_job` from `app.core.redis`.
- Produces: side effect only — one `enqueue_job(sync_pattern_to_central, pattern_id, queue_name="default")` per successful capture that has a `source_provider_id`.

- [ ] **Step 1: Write the failing test**

Create `src/backend/tests/test_pattern_capture_sync_enqueue.py`:

```python
from unittest.mock import patch

from app.services import pattern_service


def test_enqueue_sync_after_capture_helper():
    with patch.object(pattern_service, "enqueue_job") as enq:
        pattern_service._enqueue_pattern_sync("pat-123", "prov-1")
        enq.assert_called_once()
        args, kwargs = enq.call_args
        assert args[0] is pattern_service.sync_pattern_to_central
        assert args[1] == "pat-123"
        assert kwargs.get("queue_name") == "default"


def test_no_enqueue_without_source_provider():
    with patch.object(pattern_service, "enqueue_job") as enq:
        pattern_service._enqueue_pattern_sync("pat-123", None)
        enq.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_pattern_capture_sync_enqueue.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_enqueue_pattern_sync'`.

- [ ] **Step 3: Add the helper and its imports, then call it after capture commit**

At the top of `src/backend/app/services/pattern_service.py`, add imports near the other `app.core` / `app.services` imports:

```python
from app.core.redis import enqueue_job
from app.services.pattern_sync import sync_pattern_to_central
```

Add the helper (place it just above the function that contains the capture-completion block):

```python
def _enqueue_pattern_sync(pattern_id: str, source_provider_id: str | None) -> None:
    """Kick off eager OBC->central S4 replication for a freshly captured pattern."""
    if not source_provider_id:
        return
    enqueue_job(sync_pattern_to_central, pattern_id, queue_name="default")
```

Then, in the capture-completion block right after `notify_pattern(pattern_id, {"type": "capture-complete"})` (currently line 834):

```python
    _clear_capture_progress(pattern_id)
    notify_pattern(pattern_id, {"type": "capture-complete"})
    _enqueue_pattern_sync(pattern_id, pattern.source_provider_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_pattern_capture_sync_enqueue.py -v`
Expected: PASS.

- [ ] **Step 5: Run the existing pattern-service tests to confirm no regression**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/ -k pattern -v`
Expected: PASS (no import-time or capture-path regressions).

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka
black src/backend/app/services/pattern_service.py src/backend/tests/test_pattern_capture_sync_enqueue.py
git add src/backend/app/services/pattern_service.py src/backend/tests/test_pattern_capture_sync_enqueue.py
git commit -m "feat(patterns): enqueue OBC->central sync on capture completion"
```

---

### Task 5: Deploy per-disk source selection + central pre-flight

Replace the primary-vs-gold guesswork in deploy with PatternLocation-driven per-disk source selection against the target cluster, and add a live pre-flight HEAD for central-source disks before the CR is created.

**Files:**
- Modify: `src/backend/app/services/deploy_service.py` — rework `_resolve_pattern_disk` (1731-1766), update `_resolve_disk_s3_paths` (1801-1837) to thread the target provider, add `_preflight_verify_pattern_disks`, call it in `_deploy_kubevirt_native` (after `_resolve_disk_s3_paths`, before CR creation at ~2586).
- Test: `src/backend/tests/test_deploy_source_selection.py`

**Interfaces:**
- Consumes: `pattern_disk_source_for_cluster` (Task 2); `PatternDisk` model; existing `_setup_kubevirt_s3_clients()` return tuple.
- Produces:
  - Reworked `_resolve_pattern_disk(data, db, target_provider_id)` — sets `data["resolvedS3Path"]`, `data["diskSource"]` (`"obc"|"central"`), and `data["size"]` from `PatternDisk.virtual_size_bytes`; raises `DeployError` when the disk resolves to no source.
  - `_preflight_verify_pattern_disks(topology, s3_client, bucket, s3_op) -> None` — head_object every `diskSource == "central"` pattern disk against central S4; raises `DeployError("pattern disk <label> not found in central S4 — storage not ready")` on miss.
  - A module-level `DeployError(Exception)` (add if not already present — grep first; if a deploy-specific exception already exists, reuse it).

- [ ] **Step 1: Write the failing test**

Create `src/backend/tests/test_deploy_source_selection.py`:

```python
import uuid
from unittest.mock import MagicMock

import pytest

from app.models.pattern import Pattern, PatternDisk
from app.models.pattern_location import PatternLocation
from app.models.user import User
from app.services import deploy_service


def _pattern_disk(db, virtual_bytes=21474836480):
    user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4()}@e.com", name="t")
    db.add(user)
    db.flush()
    pat = Pattern(id=str(uuid.uuid4()), name="p", owner_id=user.id, topology={})
    db.add(pat)
    db.flush()
    pd = PatternDisk(
        id=str(uuid.uuid4()),
        pattern_id=pat.id,
        source_disk_id="d",
        source_vm_id="v",
        s3_key="patterns/p/d.qcow2",
        format="qcow2",
        size_bytes=5000,
        virtual_size_bytes=virtual_bytes,
        state="available",
    )
    db.add(pd)
    db.flush()
    return pat, pd


PROV = str(uuid.uuid4())


def _data(pat, pd):
    return {
        "source": "pattern",
        "patternId": pat.id,
        "patternDiskId": pd.id,
        "label": "boot",
        "size": 10,
    }


def test_resolve_prefers_obc_on_source_provider(db_session):
    pat, pd = _pattern_disk(db_session)
    db_session.add(
        PatternLocation(
            pattern_disk_id=pd.id, provider_id=PROV, location_type="obc",
            s3_key=pd.s3_key, state="synced",
        )
    )
    db_session.flush()
    data = _data(pat, pd)
    deploy_service._resolve_pattern_disk(data, db_session, PROV)
    assert data["diskSource"] == "obc"
    assert data["resolvedS3Path"] == pd.s3_key
    # 20 GiB virtual -> size bumped to at least 20
    assert data["size"] >= 20


def test_resolve_uses_central_when_no_obc(db_session):
    pat, pd = _pattern_disk(db_session)
    db_session.add(
        PatternLocation(
            pattern_disk_id=pd.id, provider_id=None, location_type="central",
            s3_key=pd.s3_key, state="synced",
        )
    )
    db_session.flush()
    data = _data(pat, pd)
    deploy_service._resolve_pattern_disk(data, db_session, PROV)
    assert data["diskSource"] == "central"


def test_resolve_raises_when_nowhere(db_session):
    pat, pd = _pattern_disk(db_session)
    data = _data(pat, pd)
    with pytest.raises(deploy_service.DeployError):
        deploy_service._resolve_pattern_disk(data, db_session, PROV)


def test_preflight_raises_on_central_miss():
    s3_client = MagicMock()
    s3_client.head_object.side_effect = Exception("404 NoSuchKey")
    topology = {
        "nodes": [
            {
                "type": "storageNode",
                "data": {
                    "source": "pattern",
                    "diskSource": "central",
                    "resolvedS3Path": "patterns/p/d.qcow2",
                    "label": "boot",
                },
            }
        ]
    }
    with pytest.raises(deploy_service.DeployError) as ei:
        deploy_service._preflight_verify_pattern_disks(
            topology, s3_client, "troshka-images", {}
        )
    assert "boot" in str(ei.value)


def test_preflight_skips_obc_disks():
    s3_client = MagicMock()
    topology = {
        "nodes": [
            {
                "type": "storageNode",
                "data": {
                    "source": "pattern",
                    "diskSource": "obc",
                    "resolvedS3Path": "patterns/p/d.qcow2",
                    "label": "boot",
                },
            }
        ]
    }
    deploy_service._preflight_verify_pattern_disks(
        topology, s3_client, "troshka-images", {}
    )
    s3_client.head_object.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_deploy_source_selection.py -v`
Expected: FAIL — `_resolve_pattern_disk` has the old signature / `DeployError` and `_preflight_verify_pattern_disks` do not exist.

- [ ] **Step 3: Implement source selection and pre-flight**

First grep for an existing deploy exception: `grep -n "class .*Error" src/backend/app/services/deploy_service.py`. If none suitable, add near the top of `deploy_service.py`:

```python
class DeployError(Exception):
    """Raised when a deploy cannot proceed (e.g. pattern storage not ready)."""
```

Replace `_resolve_pattern_disk` (currently 1731-1766) with:

```python
def _resolve_pattern_disk(data, db, target_provider_id):
    """Resolve source (obc|central) + S3 path for a pattern-sourced disk.

    Uses PatternLocation on the target cluster. Raises DeployError if the disk
    is not synced anywhere reachable from that cluster (placement should
    prevent this; this is the correctness backstop).
    """
    import math

    from app.models.pattern import PatternDisk as PatternDiskModel
    from app.services.pattern_locations import pattern_disk_source_for_cluster

    pid = data["patternId"]
    pattern_disk_id = data.get("patternDiskId", "")
    pd_record = (
        db.query(PatternDiskModel).filter_by(id=pattern_disk_id, pattern_id=pid).first()
        if pattern_disk_id
        else None
    )
    if pd_record and pd_record.s3_key:
        s3_path = pd_record.s3_key
    else:
        s3_path = f"patterns/{pid}/{pattern_disk_id}.qcow2"

    source = pattern_disk_source_for_cluster(db, pattern_disk_id, target_provider_id)
    if source is None:
        raise DeployError(
            f"pattern disk {data.get('label', pattern_disk_id[:8])} is not available "
            f"on the target cluster — storage not ready"
        )

    data["resolvedS3Path"] = s3_path
    data["diskSource"] = source
    logger.info(
        "Deploy: pattern disk %s s3=%s source=%s",
        data.get("label", "?"),
        s3_path[:40],
        source,
    )
    if pd_record and pd_record.virtual_size_bytes:
        real_gb = math.ceil(pd_record.virtual_size_bytes / (1024**3))
        if real_gb > (data.get("size", 0) or 0):
            data["size"] = real_gb
```

Update `_resolve_disk_s3_paths` (1801-1837) to accept and pass `target_provider_id` to `_resolve_pattern_disk`, and to no longer pass the S3 clients to it:

```python
def _resolve_disk_s3_paths(
    topology,
    db,
    target_provider_id,
    s3_client,
    bucket,
    s3_op,
    central_s3_client,
    central_bucket,
    central_op,
):
    """Resolve S3 paths for pattern and library disks, annotating topology nodes."""
    for node in topology.get("nodes", []):
        data = node.get("data", {})
        if node.get("type") != "storageNode":
            continue
        if data.get("source") == "pattern" and data.get("patternId"):
            _resolve_pattern_disk(data, db, target_provider_id)
        elif data.get("source") == "library" and data.get("libraryItemId"):
            _resolve_library_disk(
                data, db, s3_client, bucket, s3_op,
                central_s3_client, central_bucket, central_op,
            )
```

Add the pre-flight helper (place near `_resolve_disk_s3_paths`):

```python
def _preflight_verify_pattern_disks(topology, s3_client, bucket, s3_op):
    """HEAD every central-source pattern disk against central S4 before deploy.

    OBC-source disks are trusted (their synced PatternLocation was written only
    after a verified capture, and the OBC endpoint is unreachable from here).
    """
    for node in topology.get("nodes", []):
        data = node.get("data", {})
        if node.get("type") != "storageNode":
            continue
        if data.get("source") != "pattern":
            continue
        if data.get("diskSource") != "central":
            continue
        key = data.get("resolvedS3Path", "")
        try:
            s3_client.head_object(Bucket=bucket, Key=key, **s3_op)
        except Exception as e:
            raise DeployError(
                f"pattern disk {data.get('label', key[:16])} not found in "
                f"central S4 — storage not ready"
            ) from e
```

- [ ] **Step 4: Wire target provider + pre-flight into `_deploy_kubevirt_native`**

In `_deploy_kubevirt_native` (2560+), update the `_resolve_disk_s3_paths` call (2586-2595) to pass the target provider (`host.provider_id`), and add the pre-flight call immediately after, wrapping both in the deploy error path:

```python
    try:
        _resolve_disk_s3_paths(
            topology,
            db,
            host.provider_id,
            s3_client,
            bucket,
            s3_op,
            central_s3_client,
            central_bucket,
            central_op,
        )
        _preflight_verify_pattern_disks(topology, s3_client, bucket, s3_op)
    except DeployError as e:
        project.state = "error"
        project.deploy_error = str(e)
        db.commit()
        logger.warning("Deploy %s aborted: %s", project_id[:8], e)
        return
```

> `host` is a parameter of `_deploy_kubevirt_native(project_id, project, host, topology, db)`, and `host.provider_id` is the target cluster's provider. Confirm no other caller passes the old 9-arg signature of `_resolve_disk_s3_paths` — grep `_resolve_disk_s3_paths(` to be sure this is the only call site.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_deploy_source_selection.py -v`
Expected: PASS (all 5 tests).

- [ ] **Step 6: Run the deploy-service test suite for regressions**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/ -k "deploy" -v`
Expected: PASS. If any test referenced the old `centralSource` key or the old `_resolve_pattern_disk` signature, update it to the new `diskSource` contract.

- [ ] **Step 7: Commit**

```bash
cd /Users/prutledg/troshka
black src/backend/app/services/deploy_service.py src/backend/tests/test_deploy_source_selection.py
git add src/backend/app/services/deploy_service.py src/backend/tests/test_deploy_source_selection.py
git commit -m "feat(deploy): PatternLocation-driven per-disk source selection with central pre-flight"
```

---

### Task 6: Operator routes each pattern disk to OBC or central

Carry the per-disk `source` through the CR topology and make the operator's golden-PVC creation use the local OBC config for `source == "obc"` disks and central S4 otherwise.

**Files:**
- Modify: `src/operator/helpers/topology.py:237-246` (`_build_disk_from_storage`, pattern branch — add `source`).
- Modify: `src/operator/handlers/vm.py:196-214` (`_resolve_disk_s3` — add obc branch).
- Modify: `src/operator/handlers/project.py:1023-1036` (`_create_golden_pvc_for_disk` — source-driven).
- Test: `src/operator/tests/test_operator_source_routing.py`

**Interfaces:**
- Consumes: CR topology `data["diskSource"]` (Task 5); `s3Config.obcConfig` (already built in `kubevirt.py:1013-1018`); `centralS3Config` = gold (unchanged).
- Produces: disk dict `patternImage["source"]: "obc"|"central"`; `_resolve_disk_s3` returns the OBC config + `s3-obc-credentials` for obc-source pattern disks.

- [ ] **Step 1: Write the failing test**

Create `src/operator/tests/test_operator_source_routing.py`:

```python
from helpers.topology import _build_disk_from_storage
from handlers.vm import _resolve_disk_s3


def test_pattern_disk_carries_source():
    sd = {
        "source": "pattern",
        "patternId": "pat-1",
        "patternDiskId": "pd-1",
        "resolvedS3Path": "patterns/pat-1/pd-1.qcow2",
        "diskSource": "obc",
        "format": "qcow2",
        "size": 20,
    }
    result = _build_disk_from_storage(sd, "storage-1")
    assert result["disk"]["patternImage"]["source"] == "obc"


def test_pattern_disk_defaults_source_central():
    sd = {
        "source": "pattern",
        "patternId": "pat-1",
        "patternDiskId": "pd-1",
        "resolvedS3Path": "patterns/pat-1/pd-1.qcow2",
        "format": "qcow2",
        "size": 20,
    }
    result = _build_disk_from_storage(sd, "storage-1")
    assert result["disk"]["patternImage"]["source"] == "central"


def test_resolve_obc_source_uses_obc_config():
    s3_config = {
        "bucket": "troshka-images",
        "endpoint": "https://s4",
        "obcConfig": {
            "bucket": "obc-bucket",
            "endpoint": "https://rgw.svc",
            "credentialsSecret": "s3-obc-credentials",
        },
    }
    disk = {"patternImage": {"s3Path": "patterns/p/d.qcow2", "source": "obc"}}
    path, cfg, secret = _resolve_disk_s3(disk, s3_config, None)
    assert path == "patterns/p/d.qcow2"
    assert cfg["bucket"] == "obc-bucket"
    assert secret == "s3-obc-credentials"


def test_resolve_central_source_uses_primary():
    s3_config = {"bucket": "troshka-images", "endpoint": "https://s4"}
    disk = {"patternImage": {"s3Path": "patterns/p/d.qcow2", "source": "central"}}
    path, cfg, secret = _resolve_disk_s3(disk, s3_config, {"bucket": "gold"})
    assert cfg["bucket"] == "troshka-images"
    assert secret == "s3-credentials"


def test_resolve_obc_source_falls_back_when_no_obc_config():
    s3_config = {"bucket": "troshka-images", "endpoint": "https://s4"}
    disk = {"patternImage": {"s3Path": "patterns/p/d.qcow2", "source": "obc"}}
    path, cfg, secret = _resolve_disk_s3(disk, s3_config, None)
    assert cfg["bucket"] == "troshka-images"
    assert secret == "s3-credentials"
```

> Operator tests import from `helpers.` / `handlers.` (not `src.operator.`). Confirm how the operator test suite is invoked and run it that way in Step 2 (see the existing `src/operator/tests/` for the exact command / conftest path setup).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/prutledg/troshka/src/operator && ./venv/bin/python3 -m pytest tests/test_operator_source_routing.py -v` (adjust interpreter/path to match how the operator suite normally runs — check `src/operator/tests/conftest.py`).
Expected: FAIL — `patternImage` has no `source` key; `_resolve_disk_s3` ignores `source`.

- [ ] **Step 3: Carry source in `_build_disk_from_storage`**

In `src/operator/helpers/topology.py`, the pattern branch (237-246) becomes:

```python
    if source_type == "pattern":
        pattern_id = sd.get("patternId", "")
        disk_id = sd.get("patternDiskId", "")
        resolved_path = sd.get("resolvedS3Path", "")
        if pattern_id and (disk_id or resolved_path):
            disk["patternImage"] = {
                "s3Path": resolved_path or f"patterns/{pattern_id}/{disk_id}.qcow2",
                "format": "qcow2",
                "central": central,
                "source": sd.get("diskSource", "central"),
            }
```

- [ ] **Step 4: Add the obc branch to `_resolve_disk_s3` (vm.py)**

Replace `_resolve_disk_s3` (196-214) with:

```python
def _resolve_disk_s3(disk, s3_config, central_s3_config):
    """Return (s3_path, s3_config_dict, secret_name) for a disk, or (None, None, None)."""
    s3_path = None
    use_central = False
    pattern_source = None
    if disk.get("libraryImage", {}).get("s3Path"):
        s3_path = disk["libraryImage"]["s3Path"]
        use_central = disk["libraryImage"].get("central", False)
    elif disk.get("patternImage", {}).get("s3Path"):
        s3_path = disk["patternImage"]["s3Path"]
        pattern_source = disk["patternImage"].get("source", "central")
    if not s3_path:
        return None, None, None
    if pattern_source == "obc":
        obc = (s3_config or {}).get("obcConfig")
        if obc:
            return (
                s3_path,
                obc,
                obc.get("credentialsSecret", "s3-obc-credentials"),  # pragma: allowlist secret  # NOSONAR
            )
    if use_central and central_s3_config:
        return (
            s3_path,
            central_s3_config,
            "s3-central-credentials",  # pragma: allowlist secret  # NOSONAR
        )
    return s3_path, s3_config, "s3-credentials"  # pragma: allowlist secret  # NOSONAR
```

- [ ] **Step 5: Make `_create_golden_pvc_for_disk` source-driven (project.py)**

Replace the config-selection block (1023-1036) with:

```python
    # Pattern disks carry an explicit source: obc (local RGW) or central (S4).
    pattern_source = None
    if disk.get("patternImage"):
        pattern_source = disk["patternImage"].get("source", "central")
    obc_config = s3_config.get("obcConfig")
    if pattern_source == "obc" and obc_config:
        disk_s3_config = obc_config
        secret_name = obc_config.get(
            "credentialsSecret", "s3-obc-credentials"  # pragma: allowlist secret
        )
    elif use_central and central_s3_config:
        disk_s3_config = central_s3_config
        secret_name = "s3-central-credentials"  # pragma: allowlist secret
    else:
        disk_s3_config = s3_config
        secret_name = "s3-credentials"  # pragma: allowlist secret
```

> The `is_pattern = disk.get("patternImage") is not None` line above this block (1024) is now unused — remove it. `use_central` still comes from `_resolve_disk_s3_path(disk)` and remains `False` for pattern disks (they no longer set `patternImage.central=True`), so the `elif use_central` gold branch never fires for patterns. Confirm by re-reading `_resolve_disk_s3_path` at 1090.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /Users/prutledg/troshka/src/operator && ./venv/bin/python3 -m pytest tests/test_operator_source_routing.py -v`
Expected: PASS (all 5 tests).

- [ ] **Step 7: Run the operator suite for regressions**

Run: `cd /Users/prutledg/troshka/src/operator && ./venv/bin/python3 -m pytest tests/ -k "disk or pattern or golden" -v`
Expected: PASS. Existing tests that assert `patternImage["central"]` still pass (the key is retained); update any test that asserted the old `is_pattern and obc_config` always-OBC behavior to the new source-driven contract.

- [ ] **Step 8: Commit**

```bash
cd /Users/prutledg/troshka
black src/operator/helpers/topology.py src/operator/handlers/vm.py src/operator/handlers/project.py src/operator/tests/test_operator_source_routing.py
git add src/operator/helpers/topology.py src/operator/handlers/vm.py src/operator/handlers/project.py src/operator/tests/test_operator_source_routing.py
git commit -m "feat(operator): route pattern disks to OBC or central S4 by per-disk source"
```

---

### Task 7: Placement filters to storage-ready clusters

Make placement skip clusters where the pattern's storage is not fully available, and fail fast with a message that distinguishes "still syncing" from "no capacity."

**Files:**
- Modify: `src/backend/app/services/placement.py` — add `pattern_disk_ids` param to `find_available_host` (162-211) and thread it from `_select_host` (481, 490); add readiness pre-check + distinct messages in `place_project` (652-686).
- Modify: `src/backend/app/services/deploy_service.py:3833` (`_deploy_resolve_host`) — thread pattern disk ids.
- Test: `src/backend/tests/test_placement_storage_readiness.py`

**Interfaces:**
- Consumes: `pattern_disk_ids_from_topology`, `pattern_disks_ready_on_provider` (Task 2).
- Produces:
  - `find_available_host(..., pattern_disk_ids: list[str] | None = None)` — candidates filtered by `pattern_disks_ready_on_provider(db, pattern_disk_ids, host.provider_id)`.
  - `_storage_ready_anywhere(db, pattern_disk_ids) -> bool` — True if some active connected host's provider makes all disks ready.
  - `place_project` returns `{"error": "pattern storage still syncing to central S4 — try again shortly"}` when storage is the only blocker.

- [ ] **Step 1: Write the failing test**

Create `src/backend/tests/test_placement_storage_readiness.py`:

```python
import uuid

from app.models.host import Host
from app.models.pattern import Pattern, PatternDisk
from app.models.pattern_location import PatternLocation
from app.models.project import Project
from app.models.user import User
from app.services import placement


def _host(db, provider_id, ram=64000, vcpus=32):
    h = Host(
        id=str(uuid.uuid4()),
        state="active",
        agent_status="connected",
        host_type="kubevirt-cluster",
        provider_id=provider_id,
        total_vcpus=vcpus,
        total_ram_mb=ram,
        used_vcpus=0,
        used_ram_mb=0,
        max_eips=0,
        ip_address="10.0.0.1",
    )
    db.add(h)
    db.flush()
    return h


def _pattern_disk(db):
    user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4()}@e.com", name="t")
    db.add(user)
    db.flush()
    pat = Pattern(id=str(uuid.uuid4()), name="p", owner_id=user.id, topology={})
    db.add(pat)
    db.flush()
    pd = PatternDisk(
        id=str(uuid.uuid4()), pattern_id=pat.id, source_disk_id="d",
        source_vm_id="v", s3_key="patterns/p/d.qcow2", format="qcow2",
        size_bytes=100, state="available",
    )
    db.add(pd)
    db.flush()
    return pd


def test_find_host_skips_unready_cluster(db_session):
    prov = str(uuid.uuid4())
    _host(db_session, prov)
    pd = _pattern_disk(db_session)  # no location anywhere -> unready
    host = placement.find_available_host(
        db_session, 4, 8000, pattern_disk_ids=[pd.id]
    )
    assert host is None


def test_find_host_allows_ready_cluster(db_session):
    prov = str(uuid.uuid4())
    h = _host(db_session, prov)
    pd = _pattern_disk(db_session)
    db_session.add(
        PatternLocation(
            pattern_disk_id=pd.id, provider_id=None, location_type="central",
            s3_key=pd.s3_key, state="synced",
        )
    )
    db_session.flush()
    host = placement.find_available_host(
        db_session, 4, 8000, pattern_disk_ids=[pd.id]
    )
    assert host is not None
    assert host.id == h.id


def test_storage_ready_anywhere(db_session):
    prov = str(uuid.uuid4())
    _host(db_session, prov)
    pd = _pattern_disk(db_session)
    assert placement._storage_ready_anywhere(db_session, [pd.id]) is False
    db_session.add(
        PatternLocation(
            pattern_disk_id=pd.id, provider_id=prov, location_type="obc",
            s3_key=pd.s3_key, state="synced",
        )
    )
    db_session.flush()
    assert placement._storage_ready_anywhere(db_session, [pd.id]) is True


def test_place_project_syncing_message(db_session):
    prov = str(uuid.uuid4())
    _host(db_session, prov)
    pd = _pattern_disk(db_session)
    user = db_session.query(User).first()
    topo = {
        "nodes": [
            {"id": "vm1", "type": "vmNode", "data": {"vcpus": 2, "ram": 4}},
            {
                "id": "s1",
                "type": "storageNode",
                "data": {"source": "pattern", "patternId": pd.pattern_id,
                         "patternDiskId": pd.id},
            },
        ]
    }
    proj = Project(
        id=str(uuid.uuid4()), name="proj", owner_id=user.id,
        provider_id=prov, topology=topo, state="draft",
    )
    db_session.add(proj)
    db_session.flush()
    result = placement.place_project(db_session, proj)
    assert "syncing" in result.get("error", "").lower()
```

> Match `Host` / `Project` constructor kwargs to the real models — grep the model files if a column name differs (e.g. `owner_id` vs `user_id`). Adjust the fixtures rather than the assertions.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_placement_storage_readiness.py -v`
Expected: FAIL — `find_available_host` has no `pattern_disk_ids` kwarg; `_storage_ready_anywhere` missing; no syncing message.

- [ ] **Step 3: Add the readiness filter to `find_available_host`**

Update the signature and candidate loop in `placement.py` (162-211):

```python
def find_available_host(
    db: Session,
    required_vcpus: int,
    required_ram_mb: int,
    required_eips: int = 0,
    storage_pool_id: str | None = None,
    provider_id: str | None = None,
    pattern_disk_ids: list[str] | None = None,
) -> Host | None:
```

Inside the candidate loop, after the EIP check and before appending the candidate:

```python
            if pattern_disk_ids:
                from app.services.pattern_locations import (
                    pattern_disks_ready_on_provider,
                )

                if not pattern_disks_ready_on_provider(
                    db, pattern_disk_ids, host.provider_id
                ):
                    continue

            inflight = _get_inflight_deploys(host.id)
            candidates.append((host, free_vcpus, free_ram, inflight))
```

- [ ] **Step 4: Add `_storage_ready_anywhere` and thread ids through `_select_host` / `place_project`**

Add the helper near `find_available_host`:

```python
def _storage_ready_anywhere(db: Session, pattern_disk_ids: list[str]) -> bool:
    """True if any active connected host's provider makes all disks ready."""
    from app.services.pattern_locations import pattern_disks_ready_on_provider

    if not pattern_disk_ids:
        return True
    hosts = (
        db.query(Host)
        .filter(Host.state == "active", Host.agent_status == "connected")
        .all()
    )
    return any(
        pattern_disks_ready_on_provider(db, pattern_disk_ids, h.provider_id)
        for h in hosts
    )
```

Add a `pattern_disk_ids` parameter to `_select_host` and pass it into both `find_available_host` calls (481 and 490):

```python
def _select_host(
    db: Session,
    project: Project,
    reqs: dict,
    has_anti_affinity: bool,
    storage_pool_id: str | None,
    host_id: str | None,
    pattern_disk_ids: list[str] | None = None,
) -> tuple[Host | None, str | None, dict | None]:
```

In both `find_available_host(...)` calls inside `_select_host`, add `pattern_disk_ids=pattern_disk_ids`.

In `place_project` (652-686), compute the ids and add the fail-fast readiness gate before host selection:

```python
def place_project(
    db: Session,
    project: Project,
    storage_pool_id: str | None = None,
    host_id: str | None = None,
) -> dict:
    """Assign a project to a host. Returns placement result."""
    if not project.topology:
        return {"error": "Project has no topology"}

    reqs = calculate_project_requirements(project.topology)
    if reqs["vm_count"] == 0:
        return {"error": "Project has no VMs"}

    from app.services.pattern_locations import pattern_disk_ids_from_topology

    pattern_disk_ids = pattern_disk_ids_from_topology(project.topology)
    if (
        pattern_disk_ids
        and not host_id
        and not _storage_ready_anywhere(db, pattern_disk_ids)
    ):
        return {
            "error": "pattern storage still syncing to central S4 — try again shortly"
        }

    has_anti_affinity = _has_anti_affinity(project.topology)

    host, storage_pool_id, error = _select_host(
        db, project, reqs, has_anti_affinity, storage_pool_id, host_id, pattern_disk_ids
    )
```

The remainder of `place_project` is unchanged.

- [ ] **Step 5: Thread ids into the deploy-time fallback `_deploy_resolve_host`**

In `src/backend/app/services/deploy_service.py:3826-3833`, update the fallback placement call:

```python
    if not host and not project.host_id:
        from app.services.placement import (
            calculate_project_requirements,
            find_available_host,
        )
        from app.services.pattern_locations import pattern_disk_ids_from_topology

        reqs = calculate_project_requirements(project.topology or {})
        pattern_disk_ids = pattern_disk_ids_from_topology(project.topology or {})
        host = find_available_host(
            s,
            reqs["total_vcpus"],
            reqs["total_ram_mb"],
            pattern_disk_ids=pattern_disk_ids,
        )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_placement_storage_readiness.py -v`
Expected: PASS (all 5 tests).

- [ ] **Step 7: Run the placement suite for regressions**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/ -k "placement or place" -v`
Expected: PASS. Non-pattern projects (empty `pattern_disk_ids`) are unaffected because the filter and gate are skipped when the list is empty.

- [ ] **Step 8: Commit**

```bash
cd /Users/prutledg/troshka
black src/backend/app/services/placement.py src/backend/app/services/deploy_service.py src/backend/tests/test_placement_storage_readiness.py
git add src/backend/app/services/placement.py src/backend/app/services/deploy_service.py src/backend/tests/test_placement_storage_readiness.py
git commit -m "feat(placement): filter to storage-ready clusters with distinct fail-fast messages"
```

---

### Task 8: One-time backfill of central PatternLocation rows

Populate `location_type="central"` rows for legacy patterns already present in central `troshka-images`, and enqueue a sync for any pattern whose disks are only in OBC (OSAC3).

**Files:**
- Create: `src/backend/scripts/backfill_pattern_locations.py`
- Test: `src/backend/tests/test_backfill_pattern_locations.py`

**Interfaces:**
- Consumes: `PatternDisk`, `PatternLocation`; `_get_s3_client()` / `_bucket()` from `app.services.s3_storage`; `sync_pattern_to_central` + `enqueue_job` (Tasks 3-4).
- Produces:
  - `backfill_central_locations(db, s3_client, bucket) -> tuple[int, list[str]]` — returns `(rows_created, pattern_ids_needing_sync)`. For each PatternDisk with no synced central row: if `head_object` succeeds → create a synced central row; else record the pattern id as needing sync.
  - `run_backfill()` — script entrypoint: opens a session, runs `backfill_central_locations`, enqueues one `sync_pattern_to_central` per pattern id needing sync, logs a summary.

- [ ] **Step 1: Write the failing test**

Create `src/backend/tests/test_backfill_pattern_locations.py`:

```python
import uuid
from unittest.mock import MagicMock

from app.models.pattern import Pattern, PatternDisk
from app.models.pattern_location import PatternLocation
from app.models.user import User
from app.scripts.backfill_pattern_locations import backfill_central_locations


def _pattern(db, key):
    user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4()}@e.com", name="t")
    db.add(user)
    db.flush()
    pat = Pattern(id=str(uuid.uuid4()), name="p", owner_id=user.id, topology={})
    db.add(pat)
    db.flush()
    pd = PatternDisk(
        id=str(uuid.uuid4()), pattern_id=pat.id, source_disk_id="d",
        source_vm_id="v", s3_key=key, format="qcow2", size_bytes=100,
        state="available",
    )
    db.add(pd)
    db.flush()
    return pat, pd


def test_present_disk_gets_central_row(db_session):
    pat, pd = _pattern(db_session, "patterns/legacy/d.qcow2")
    s3 = MagicMock()  # head_object succeeds
    created, need_sync = backfill_central_locations(db_session, s3, "troshka-images")
    assert created == 1
    assert need_sync == []
    row = (
        db_session.query(PatternLocation)
        .filter_by(pattern_disk_id=pd.id, location_type="central")
        .first()
    )
    assert row.state == "synced"
    assert row.provider_id is None


def test_missing_disk_flags_sync(db_session):
    pat, pd = _pattern(db_session, "patterns/osac3/d.qcow2")
    s3 = MagicMock()
    s3.head_object.side_effect = Exception("404")
    created, need_sync = backfill_central_locations(db_session, s3, "troshka-images")
    assert created == 0
    assert pat.id in need_sync


def test_existing_synced_central_is_skipped(db_session):
    pat, pd = _pattern(db_session, "patterns/legacy/d.qcow2")
    db_session.add(
        PatternLocation(
            pattern_disk_id=pd.id, provider_id=None, location_type="central",
            s3_key=pd.s3_key, state="synced",
        )
    )
    db_session.flush()
    s3 = MagicMock()
    created, need_sync = backfill_central_locations(db_session, s3, "troshka-images")
    assert created == 0
    assert need_sync == []
    s3.head_object.assert_not_called()
```

> The test imports from `app.scripts.backfill_pattern_locations`. Create the file under a package importable as `app.scripts` — if `src/backend/app/scripts/` does not exist, create it with an `__init__.py`. (Placing the script under `app/` keeps it importable by the test; the `run_backfill()` entrypoint is still invoked as a module.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_backfill_pattern_locations.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Write the script**

Create `src/backend/app/scripts/__init__.py` (empty) if missing, then `src/backend/app/scripts/backfill_pattern_locations.py`:

```python
"""One-time backfill: central PatternLocation rows for legacy patterns.

For every PatternDisk without a synced central location, HEAD the object in
central S4 (troshka-images). Present -> create a synced central row (DB-only,
instant). Absent -> the pattern's disks are OBC-only; enqueue a sync.
"""

from __future__ import annotations

import datetime
import logging

from app.models.pattern import PatternDisk
from app.models.pattern_location import PatternLocation

log = logging.getLogger(__name__)


def backfill_central_locations(db, s3_client, bucket) -> tuple[int, list[str]]:
    """Create synced central rows for disks present in central S4.

    Returns (rows_created, pattern_ids_needing_sync).
    """
    created = 0
    need_sync: list[str] = []
    now = datetime.datetime.now(datetime.UTC)

    for pd in db.query(PatternDisk).all():
        existing = (
            db.query(PatternLocation)
            .filter_by(pattern_disk_id=pd.id, location_type="central", state="synced")
            .first()
        )
        if existing:
            continue
        try:
            s3_client.head_object(Bucket=bucket, Key=pd.s3_key)
        except Exception:
            if pd.pattern_id not in need_sync:
                need_sync.append(pd.pattern_id)
            continue
        db.add(
            PatternLocation(
                pattern_disk_id=pd.id,
                provider_id=None,
                location_type="central",
                s3_key=pd.s3_key,
                state="synced",
                synced_at=now,
                size_bytes=pd.size_bytes or 0,
            )
        )
        created += 1

    db.commit()
    return created, need_sync


def run_backfill() -> None:
    from app.core.database import SessionLocal
    from app.core.redis import enqueue_job
    from app.services.pattern_sync import sync_pattern_to_central
    from app.services.s3_storage import _bucket, _get_s3_client

    db = SessionLocal()
    try:
        created, need_sync = backfill_central_locations(
            db, _get_s3_client(), _bucket()
        )
        for pattern_id in need_sync:
            enqueue_job(sync_pattern_to_central, pattern_id, queue_name="default")
        log.info(
            "Backfill complete: %d central rows created, %d patterns enqueued for sync",
            created,
            len(need_sync),
        )
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_backfill()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_backfill_pattern_locations.py -v`
Expected: PASS (all 3 tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka
black src/backend/app/scripts/backfill_pattern_locations.py src/backend/tests/test_backfill_pattern_locations.py
git add src/backend/app/scripts/__init__.py src/backend/app/scripts/backfill_pattern_locations.py src/backend/tests/test_backfill_pattern_locations.py
git commit -m "feat(patterns): one-time backfill of central PatternLocation rows"
```

---

## Post-implementation (manual, after all tasks land and deploy)

These are operational steps for the human operator — not code tasks. Run after the branch is merged and the backend/operator images are deployed to prod (`infra01`).

1. **Apply the migration** — happens automatically on backend startup (Alembic runs at boot; do not run manually). Verify `pattern_locations.location_type` exists on prod.
2. **Run the backfill once** — against the prod backend pod:
   `oc exec <backend-pod> -- python3 -m app.scripts.backfill_pattern_locations`
   Expect ~19 central rows created (4 legacy patterns) and 1 pattern (OSAC3) enqueued for sync.
3. **Confirm OSAC3 sync** — watch the sync RQ job and the rclone Job on the source cluster; verify OSAC3's 7 disks land in `troshka-images` and their central rows flip to `synced`.
4. **Re-test the OSAC3 deploy** — deploy OSAC3 on its source cluster (obc path) and on a different cluster (central path); both should pass pre-flight and pull without 404 crash-loops.
5. **Set `central_s4.max_bytes`** in prod config to the real ceiling once central S4 raw capacity is provisioned (infra task; currently `null` = guard disabled).

---

## Self-Review

**1. Spec coverage:**
- §1 Data model & readiness predicate → Task 1 (columns + migration) + Task 2 (predicates). ✅
- §2 Eager capture → central sync (trigger, Job-on-source, capacity guard, states, idempotency) → Task 3 (worker + guard + rclone Job + idempotent skip) + Task 4 (enqueue on capture). ✅
- §3a Backend per-disk source selection → Task 5 (`_resolve_pattern_disk` rework, `data["diskSource"]`). ✅
- §3b Pre-flight verification → Task 5 (`_preflight_verify_pattern_disks`). Scoped to central-source disks per the reachability constraint (OBC unreachable from workers); documented in Global Constraints and the task. ✅
- §3c Operator routing (`_resolve_disk_s3` obc branch + CR `patternImage.source`) → Task 6. ✅
- §4 Placement storage-awareness (filter + distinct messages + non-pattern unaffected) → Task 7. ✅
- §5 Backfill (legacy central rows + OSAC3 sync enqueue, uniform PatternLocation path) → Task 8. ✅
- Error handling (sync errors on PatternLocation, deploy/placement messages) → Tasks 3, 5, 7. ✅
- Testing section items → covered across the per-task tests. ✅

**Deviations from spec, with rationale:**
- Spec §3b says HEAD "every pattern disk"; this plan HEADs only `central`-source disks and trusts the `synced` OBC row for `obc`-source disks, because the OBC `.svc` endpoint is unreachable from the backend workers (established during debugging). Recorded in Global Constraints.
- Spec grouped readiness helpers into `pattern_sync.py`; this plan puts pure predicates in a separate `pattern_locations.py` (testable without any k8s/S3 mocks) and keeps the worker in `pattern_sync.py`. Cleaner boundaries; same behavior.
- Spec listed `kubevirt.py` under "files touched"; no change is needed there — `obcConfig` is already attached to the CR (`kubevirt.py:1012-1018`) and per-disk source travels inside `spec.topology`. Left untouched deliberately.
- Helper names generalized from the spec's `pattern_ready_on_cluster(db, pattern_id, ...)` to a disk-id-list form (`pattern_disks_ready_on_provider(db, pattern_disk_ids, ...)`) so a project mixing disks from multiple patterns is handled correctly.

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to Task N". Every code step contains real code. Migration revision ids are alembic-generated (not placeholders); `down_revision` value is pinned to the known head `33827935d7e4`.

**3. Type consistency:** `location_type` / `diskSource` / `patternImage.source` string values are `"obc"` / `"central"` everywhere. `pattern_disk_source_for_cluster` returns `"obc" | "central" | None` consistently across Tasks 2, 5, 7. `find_available_host(..., pattern_disk_ids=...)` signature matches all call sites (Tasks 7 placement + deploy). `sync_pattern_to_central(pattern_id)` signature matches its enqueue sites (Tasks 4, 8). `build_sync_rclone_job` / `_run_rclone_job` / `central_capacity_available` names consistent within Task 3.
