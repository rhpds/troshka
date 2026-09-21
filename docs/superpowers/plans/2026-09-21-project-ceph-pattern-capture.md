# Project Ceph Pattern Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture project Ceph as sparse mon+OSD pattern disks and restore with original identity so any Ceph client in the pattern works without credential rewrite.

**Architecture:** After quiesce/freeze, parallel VolumeSnapshot → sparse qcow → S3 for mon and each OSD PVC. Pattern topology keeps `cephClusterNode` + `projectCephCapture` disk ids. On deploy, CDI/import fills PVCs from those disks, then `TroshkaCeph` runs in restore mode (skip empty bootstrap, attach existing devices, preserve `labIp`). Gate VM/consumer start on restored Ceph Ready.

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy 2 / Alembic, Rook-Ceph via operator (`troshkancephs`), existing pattern VolumeSnapshot export pipeline, CDI DataVolumes for restore.

**Spec:** `docs/superpowers/specs/2026-09-21-project-ceph-pattern-capture-design.md`

## Global Constraints

- KubeVirt-native projects only (same as project Ceph v1).
- Identity-preserving restore — no “new fsid + rewrite consumers.”
- Thin artifacts via sparse/compressed qcow; stored size ≠ `capacityGi`.
- Parallel snapshot/export with bounded concurrency (default `min(N, 4)`).
- Quiesce writers before Ceph device snapshots; unfreeze source Ceph only after all Ceph exports succeed (or fail/cleanup).
- Restored Ceph Ready before any Ceph consumer powers on.
- SonarQube cognitive complexity ≤ 15 per function; extract helpers.
- Python 3.13; pad `time.time()` mocks with trailing values.
- Prefer absolute paths for `git add` from repo root.
- No AI/Cursor attribution in commits.

---

## File map

| Path | Responsibility |
|------|----------------|
| `src/backend/alembic/versions/<rev>_pattern_disk_ceph_metadata.py` | Add `source_kind`, `source_index`, `source_pvc_name`; nullable `source_vm_id` |
| `src/backend/app/models/pattern.py` | ORM columns for Ceph disk metadata |
| `src/backend/app/services/project_ceph_pattern.py` | **New** — topology helpers: stamp/clear `projectCephCapture`, strip runtime `projectCeph`, validate capture set |
| `src/backend/app/api/patterns.py` | Remap `cephClusterNode` `networkRef` / `linkedClusters`; preserve `labIp` |
| `src/backend/app/services/pattern_service.py` | Quiesce → freeze → parallel Ceph capture → unfreeze; wire before/with VM capture |
| `src/backend/app/services/pattern_ceph_capture.py` | **New** — device inventory, freeze/unfreeze orchestration API used by capture |
| `src/operator/helpers/rook_ceph.py` | Discover mon/OSD PVCs; `build_ceph_cluster(..., restore=)` |
| `src/operator/helpers/patterns.py` / `handlers/project.py` | Parallel Ceph device snapshot+export (or dedicated annotation handler) |
| `src/operator/handlers/ceph.py` | Restore-mode reconcile path |
| `src/operator/handlers/project.py` | Pre-create restore PVCs from pattern disks; gate VM start on Ceph Ready when capture present |
| `src/backend/tests/test_project_ceph_pattern.py` | Topology + remap + metadata unit tests |
| `src/operator/tests/test_rook_ceph.py` | Restore-mode cluster builder tests |
| `src/backend/tests/test_pattern_ceph_capture.py` | Freeze inventory / failure cases (mocked) |

---

## Phase 0 — Rook restore spike

### Task 0: Prove Rook can adopt pre-filled mon+OSD PVCs

**Files:** Spike notes only (append to spec §9 or `docs/dev/project-ceph-pattern-restore.md`); no production merge required until green.

**Interfaces:**
- Produces: Documented PVC naming convention + CephCluster shape that reuses existing PVCs without wiping data (or explicit “not supported → pivot” abort).

- [ ] **Step 1: On ocpvdev01, use a tiny TroshkaCeph** (`capacityGi` 150, `osdCount` 3 or 1 if allowed) in a disposable ns; write a known RBD image; record `fsid` and PVC list:

```bash
oc -n <ns> get pvc -l app=troshka-ceph -o wide
oc -n <ns> get pvc | rg 'osd-set|mon|rook'
oc -n <ns> exec deploy/rook-ceph-tools -- ceph fsid   # or mon pod
```

- [ ] **Step 2: Soft-delete path experiment** — scale down / delete `CephCluster` **without** deleting PVCs; recreate `CephCluster` aiming to reattach same PVCs. Record whether data/`fsid` survive.

- [ ] **Step 3: Decide restore strategy** (write into spike doc):
  - **A (preferred):** Rook adopts existing `osd-set-*` / mon PVCs when recreated with matching device-set config.
  - **B:** Switch Troshka to explicit `troshka-ceph-osd-*` PVCs + Rook config that mounts prepared claims (use unused `build_osd_pvcs` path).
  - **C:** Abort identity-preserving device restore; escalate to product (do not silently fall back to rbd-import).

- [ ] **Step 4: Commit spike doc only** if A or B works; stop plan execution if C.

```bash
git add docs/dev/project-ceph-pattern-restore.md
git commit -m "$(cat <<'EOF'
Document Rook PVC-adopt findings for Ceph pattern restore.

EOF
)"
```

---

## Phase 1 — Schema and topology contracts

### Task 1: PatternDisk Ceph metadata columns

**Files:**
- Create: `src/backend/alembic/versions/<rev>_add_pattern_disk_ceph_metadata.py`
- Modify: `src/backend/app/models/pattern.py`
- Test: `src/backend/tests/test_pattern_disk_ceph_metadata.py`

**Interfaces:**
- Produces: `PatternDisk.source_kind: str | None` (`"ceph-mon"` \| `"ceph-osd"` \| `"vm"` \| None), `source_index: int | None`, `source_pvc_name: str | None`; `source_vm_id` nullable.

- [ ] **Step 1: Write failing model/round-trip test**

```python
def test_pattern_disk_accepts_ceph_metadata(db_session):
    # create Pattern + PatternDisk with source_kind="ceph-osd", source_index=0,
    # source_pvc_name="osd-set-data-0-xxxxx", source_vm_id=None
    # assert columns persist after refresh
    ...
```

- [ ] **Step 2: Run test — expect FAIL** (columns missing)

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_pattern_disk_ceph_metadata.py -v
```

- [ ] **Step 3: Alembic revision + model**

```python
# pattern.py PatternDisk additions
source_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
source_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
source_pvc_name: Mapped[str | None] = mapped_column(String(253), nullable=True)
# change source_vm_id to nullable=True
source_vm_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
```

Migration: add columns; alter `source_vm_id` nullable. Existing rows: `source_kind=NULL` (treat as VM).

- [ ] **Step 4: `alembic upgrade head` + pytest PASS**

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/alembic/versions/ src/backend/app/models/pattern.py src/backend/tests/test_pattern_disk_ceph_metadata.py
git commit -m "$(cat <<'EOF'
Add PatternDisk metadata columns for Ceph mon/OSD captures.

EOF
)"
```

---

### Task 2: Topology `projectCephCapture` helpers

**Files:**
- Create: `src/backend/app/services/project_ceph_pattern.py`
- Test: `src/backend/tests/test_project_ceph_pattern.py`

**Interfaces:**
- Produces:
  - `CEPH_SOURCE_MON = "ceph-mon"`, `CEPH_SOURCE_OSD = "ceph-osd"`
  - `strip_runtime_project_ceph(topology: dict) -> None`
  - `set_project_ceph_capture(topology: dict, *, mon_disk_id: str, osd_disk_ids: list[str]) -> None`
  - `get_project_ceph_capture(topology: dict) -> dict | None` → `{monDiskId, osdDiskIds}`
  - `validate_ceph_capture_disk_set(osd_count: int, devices: list[dict]) -> None` raises `ValueError` on mismatch

- [ ] **Step 1: Failing tests**

```python
def test_strip_runtime_project_ceph():
    topo = {"projectCeph": {"fsid": "x"}, "nodes": []}
    strip_runtime_project_ceph(topo)
    assert "projectCeph" not in topo

def test_set_and_get_project_ceph_capture():
    topo = {"nodes": []}
    set_project_ceph_capture(topo, mon_disk_id="m1", osd_disk_ids=["o0", "o1"])
    assert get_project_ceph_capture(topo) == {
        "monDiskId": "m1",
        "osdDiskIds": ["o0", "o1"],
    }

def test_validate_ceph_capture_disk_set_rejects_partial():
    with pytest.raises(ValueError, match="osd"):
        validate_ceph_capture_disk_set(3, [{"kind": "ceph-mon"}, {"kind": "ceph-osd"}])
```

- [ ] **Step 2: Implement helpers; pytest PASS; commit**

---

### Task 3: Remap `cephClusterNode` refs on pattern deploy

**Files:**
- Modify: `src/backend/app/api/patterns.py` (`_remap_topology` / new `_remap_ceph_node`)
- Test: `src/backend/tests/test_ocp_clusters.py` or `tests/test_pattern_ceph_remap.py`

**Interfaces:**
- Consumes: `id_map` from `_remap_node_ids`
- Produces: remapped `data.networkRef`; remapped `data.linkedClusters` entries through `id_map`; **`labIp` unchanged**; `projectCephCapture` disk ids unchanged (PatternDisk ids are global)

- [ ] **Step 1: Failing test** — topology with network + ceph node `networkRef` + `linkedClusters` pointing at clusterNode ids; after `_remap_topology`, refs follow new ids; `labIp` identical.

- [ ] **Step 2: Implement `_remap_ceph_node(nodes, id_map)` called from `_remap_topology`; PASS; commit**

---

## Phase 2 — Operator: inventory + restore mode

### Task 4: Discover mon and OSD PVCs for a TroshkaCeph

**Files:**
- Modify: `src/operator/helpers/rook_ceph.py`
- Test: `src/operator/tests/test_rook_ceph.py`

**Interfaces:**
- Produces: `discover_ceph_device_pvcs(core_api, namespace: str) -> list[dict]`  
  Each item: `{name, kind: "ceph-mon"|"ceph-osd", index: int, size_bytes: int}`  
  Discovery must match spike naming (Task 0). Fail if counts ≠ expected `osdCount` from CephCluster/TroshkaCeph.

- [ ] **Step 1: Unit test with mocked PVC list** (osd-set + mon claim names from spike).

- [ ] **Step 2: Implement discovery; PASS; commit**

---

### Task 5: `build_ceph_cluster` restore mode

**Files:**
- Modify: `src/operator/helpers/rook_ceph.py`
- Modify: `src/operator/handlers/ceph.py`
- Test: `src/operator/tests/test_rook_ceph.py`

**Interfaces:**
- Consumes: TroshkaCeph `spec.restore` e.g. `{ "enabled": true, "monPvc": "...", "osdPvcs": ["...", ...] }` (exact shape from Task 0 spike).
- Produces: CephCluster manifest that **does not** provision empty new device PVCs; attaches restore PVCs per spike strategy A or B.

- [ ] **Step 1: Failing test** — `build_ceph_cluster(cr_with_restore)` has no empty oversized new claims / includes restore PVC refs as documented in spike.

- [ ] **Step 2: Implement builder + handler branch when `spec.restore.enabled`; PASS; commit**

- [ ] **Step 3: CRD / openAPI** — if TroshkaCeph CRD is generated in-repo, extend schema for `spec.restore`; else document annotation fallback `troshka.redhat.com/ceph-restore: <json>` and read it in handler (prefer real spec field).

---

## Phase 3 — Capture path

### Task 6: Freeze / unfreeze helpers

**Files:**
- Create: `src/backend/app/services/pattern_ceph_capture.py` (or operator-side job invoked by annotation — prefer **operator** for ceph CLI access; backend only triggers)
- Prefer operator module: `src/operator/helpers/ceph_freeze.py`
- Test: `src/operator/tests/test_ceph_freeze.py` (mocked exec)

**Interfaces:**
- Produces: `freeze_ceph_for_capture(namespace) -> None`, `unfreeze_ceph_after_capture(namespace) -> None`  
  Sequence: `ceph osd set noout` (and related) → flush → stop OSD deployments (and mon if spike requires) so block devices idle; reverse on unfreeze. Must leave source healthy on success path.

- [ ] **Step 1: Failing unit tests for command ordering (mocked).**
- [ ] **Step 2: Implement; PASS; commit**

---

### Task 7: Parallel Ceph device snapshot + export

**Files:**
- Modify: `src/operator/handlers/project.py` and/or `helpers/patterns.py`
- Extend capture annotation payload to include `cephDevices: [{pvcName, kind, index, patternDiskId, s3Key}]`
- Test: operator unit test that N devices schedule N snapshot jobs with concurrency cap

**Interfaces:**
- Consumes: device list from Task 4; S3 creds as existing capture
- Produces: sparse qcow uploads; progress events; **fail entire Ceph capture** if any device fails
- Parallelism: per-device pipeline `snapshot → temp PVC → qemu-img convert -S / compress → rclone`; concurrency `min(len(devices), 4)` (constant `CEPH_CAPTURE_CONCURRENCY = 4`)

- [ ] **Step 1: Failing test** — mock 3 devices; assert 3 snapshot creates issued without waiting serially for export completion of device 0 before snapshot of device 1 (use barrier/spy).

- [ ] **Step 2: Implement parallel capture; on any failure mark capture error and do not partial-commit Ceph disks; PASS; commit**

---

### Task 8: Wire Ceph capture into `pattern_service`

**Files:**
- Modify: `src/backend/app/services/pattern_service.py`
- Test: `src/backend/tests/test_pattern_ceph_capture_wire.py` (heavy mocks)

**Interfaces:**
- Order inside KubeVirt capture:
  1. Existing quiesce (VMs / OCP)
  2. If `topology_has_ceph`: request operator freeze + parallel Ceph device capture
  3. On success: create `PatternDisk` rows (`source_kind`, `source_index`, `source_pvc_name`, `source_vm_id=None`, `source_disk_id=f"ceph-{kind}-{index}"`)
  4. `set_project_ceph_capture` + `strip_runtime_project_ceph` on pattern topology; persist
  5. Unfreeze Ceph
  6. Existing VM disk capture
- If Ceph present but not Ready / PVC mismatch: **fail capture** (pattern not `available`)

- [ ] **Step 1: Failing integration-style unit test** with mocked operator + DB.
- [ ] **Step 2: Implement; PASS; commit**

---

## Phase 4 — Restore / deploy gate

### Task 9: Materialize Ceph PVCs from pattern disks on deploy

**Files:**
- Modify: `src/operator/handlers/project.py` (deploy path near DataVolume creation)
- Helper: `src/operator/helpers/ceph_restore.py` (**new**)

**Interfaces:**
- Consumes: `projectCephCapture` + PatternDisk S3 paths (via topology disk descriptors stamped like VM `patternImage`)
- Produces: Bound PVCs named per Task 0 convention, `volumeMode: Block`, size ≥ `virtual_size_bytes`
- Reuse existing pattern S3 → DataVolume/import machinery where possible (block mode)

- [ ] **Step 1: Unit test** — given capture block + fake disk map, builder emits correct PVC/DV list.
- [ ] **Step 2: Implement; wait DV Succeeded before TroshkaCeph create; PASS; commit**

---

### Task 10: Create TroshkaCeph in restore mode + boot gate

**Files:**
- Modify: `src/operator/handlers/project.py` (`_create_ceph_cr`)
- Modify: deploy wait / VM start gating in project handler

**Interfaces:**
- If `projectCephCapture` present: set `spec.restore` (Task 5); else empty bootstrap (today).
- **Do not** start virt-launchers / power-on VMs that are Ceph consumers until TroshkaCeph `status.phase==Ready` when capture present.
- Practical gate: existing Ceph Ready wait used for storage-dependent steps — extend so **all VM starts** wait when restore mode is active (safest general-purpose rule: any project with restored Ceph waits Ready before VM phase).

- [ ] **Step 1: Failing test** — restore topology does not call empty bootstrap builder; VM create waits on Ready.
- [ ] **Step 2: Implement; PASS; commit**

---

## Phase 5 — Hardening

### Task 11: Failure + legacy matrix tests

**Files:**
- `src/backend/tests/test_project_ceph_pattern.py` (extend)
- `src/operator/tests/test_rook_ceph.py` (extend)

- [ ] **Step 1: Cases**
  - `cephClusterNode` without `projectCephCapture` → empty bootstrap
  - Partial OSD list → validate error
  - Remap preserves `labIp`
- [ ] **Step 2: PASS; commit**

---

### Task 12: Dev E2E checklist (manual)

Not automated in CI. Document in `docs/dev/project-ceph-pattern-restore.md`:

1. Small project Ceph + write RBD + install keyring on a lab VM  
2. Capture pattern (confirm sparse sizes ≪ capacityGi)  
3. Deploy pattern to new project  
4. Verify same `fsid`, RBD content, VM client works without secret rewrite  
5. Confirm source project Ceph healthy after capture  

- [ ] **Step 1: Run checklist on ocpvdev01; file results in doc; commit doc update**

---

## Sequencing

```
Task 0 spike ──► Task 1 schema ──► Task 2 topology ──► Task 3 remap
                      │
                      ▼
              Task 4 discover ──► Task 5 restore builder
                      │
                      ▼
              Task 6 freeze ──► Task 7 parallel export ──► Task 8 wire capture
                      │
                      ▼
              Task 9 PVC materialize ──► Task 10 gate ──► Task 11/12
```

Tasks 2–3 can parallel Task 4–5 after Task 0+1.

---

## Spec coverage (self-review)

| Spec section | Tasks |
|--------------|-------|
| Sparse mon+OSD capture | 4, 6, 7, 8 |
| Identity-preserving restore | 0, 5, 9, 10 |
| Unknown consumers / no rewrite | 0, 5, 10 (identity) |
| Parallel capture | 7 |
| Quiesce + unfreeze source | 6, 8 |
| Boot gate | 10 |
| Topology capture block + strip stamp | 2, 8 |
| Remap networkRef/linkedClusters, keep labIp | 3 |
| Legacy empty Ceph | 10, 11 |
| Failure: partial/not Ready | 8, 11 |
| Thinness warning | 12 (docs); optional UI later — out of scope unless trivial |

**Placeholder scan:** none intentional. Task 0 may force plan amendment if Rook adopt fails.

**Type consistency:** `projectCephCapture.monDiskId` / `osdDiskIds`; `source_kind` values `ceph-mon` \| `ceph-osd`; `spec.restore` shape locked by Task 0 then Task 5.
