# Troshka Workloads — Plan 3: UI (Ad-Hoc) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user trigger, watch (live progress + logs), and review ad-hoc AgnosticD-v2 workload runs entirely from the Troshka project UI, targeting either an OCP cluster or project VMs.

**Architecture:** Approach A/thin-pod is unchanged. This plan adds four small additive backend endpoints/behaviors on the shipped run API and a canvas-scoped frontend (a "Run Workload" button + three hand-rolled overlay modals). Progress/logs use "Approach 3": a polled run-log endpoint is the source of truth, with the already-emitted `workload-progress` WebSocket message as an immediate-refetch nudge.

**Tech Stack:** Backend — Python 3.13, FastAPI, SQLAlchemy 2.0, RQ/Redis, pytest (SQLite, `TestClient`). Frontend — Next.js App Router (patched), PatternFly 6 primitives, Zustand, raw `fetch`, Vitest + React Testing Library.

**Spec:** `docs/superpowers/specs/2026-09-13-troshka-workloads-plan3-ui-design.md` (and parent `docs/superpowers/specs/2026-09-11-troshka-workloads-design.md`)

**Depends on:** Plans 1 + 2 + cluster-access rework (all merged to `main`).

## Global Constraints

- **Python 3.13.** Add trailing values to any `time.time()` mocks to avoid `StopIteration`.
- **Backend tests** from `src/backend`: `./venv/bin/python3 -m pytest <path> -v`. Auth is dev-mode (auto-authenticates); reuse `tests/test_workload_api.py` helpers (`_ensure_dev_user`, `_create_project`, `_create_workload_run`, `TestSession`, `client`).
- **Frontend tests** from `src/frontend`: `npm test -- <path>` (Vitest). Seed `useCanvasStore.setState(...)` for canvas component tests; mock fetch with `vi.stubGlobal("fetch", vi.fn(...))` + `vi.unstubAllGlobals()` in `afterEach`; `@` → `src`; tests live in `__tests__/` named `*.test.tsx`.
- **Format:** backend system `black`; zero `pyright` errors in touched files (`pyright <files>` from `src/backend`). Frontend follows existing eslint/TS.
- **Cognitive complexity ≤ 15 per function** (SonarQube S3776) — extract helpers.
- **No migration.** Reuse the existing unused `WorkloadRun.log_ref` (`Text`, nullable) column for the persisted log tail — do NOT add a column or Alembic revision.
- **Secrets never in argv/logs.** No new secret-bearing surface; new endpoints are read/preview only.
- **Do NOT modify the OCP install path** or the shipped run/monitor pipeline beyond the additive edits named in Tasks 1–4.
- **Modals are hand-rolled fixed-overlay `<div>`s** with inline styles + PF CSS custom-property colors (`var(--pf-t--global--...)`), `zIndex: 10000` — the codebase does NOT use PatternFly `Modal` or `Table`. Match `SavePatternModal.tsx` / `ClusterInstallLogModal.tsx`.
- **Frontend is a patched Next.js** (`src/frontend/AGENTS.md`) — check `node_modules/next/dist/docs/` before writing Next.js-specific code.
- **Auth guard:** every new endpoint reuses `_enforce_project_access` (owner/admin; orphan run → admin-only), exactly like the shipped endpoints.
- **Git:** no `Co-Authored-By`; never amend; commit from repo root with absolute paths (`cd /Users/prutledg/troshka && git add src/...`). Work on branch `feat/workloads-plan3-ui` (not `main`).
- **After Python changes**, the backend must be restarted to take effect (no auto-reload) — note this to the user; do not auto-restart.

---

## File Structure

**Backend (all under `src/backend/`):**
- Modify `app/services/workloads/run_service.py` — persist log tail in `_finalize_workload_run`; add `get_workload_log()` + `_should_validate_inventory()`; gate validation in `run_workload_job`.
- Modify `app/services/workloads/inventory.py` — add pure `preview_inventory()`.
- Modify `app/api/workloads.py` — add `GET /workloads/{run_id}/log` and `POST /projects/{id}/workloads/inventory-preview`.
- Tests: extend `tests/test_workload_run_service.py`, `tests/test_workload_inventory.py`, `tests/test_workload_api.py`.

**Frontend (all under `src/frontend/src/`):**
- Modify `components/canvas/PropertiesPanel.tsx` — first-class "Ansible Groups" field.
- Modify `hooks/useVmStateSocket.ts` — `workload-progress` message → `workloadProgress`.
- Create `components/canvas/RunWorkloadModal.tsx` — ad-hoc trigger form.
- Create `components/canvas/WorkloadRunDetailModal.tsx` — live progress + logs.
- Create `components/canvas/WorkloadRunsModal.tsx` — run-history list.
- Modify `app/projects/[id]/page.tsx` — action-bar buttons + modal state + renders.
- Tests under `components/canvas/__tests__/` and `hooks/__tests__/`.

---

### Task 1: Persist bounded log tail on finalize

**Files:**
- Modify: `src/backend/app/services/workloads/run_service.py` (`_finalize_workload_run`, ~695-708; add module constant near top)
- Test: `src/backend/tests/test_workload_run_service.py`

**Interfaces:**
- Produces: `_finalize_workload_run(run_id, status, error_or_logs)` now also writes `run.log_ref = (error_or_logs or "")[-_LOG_TAIL_BYTES:]` for every terminal status. Constant `_LOG_TAIL_BYTES = 256 * 1024`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_workload_run_service.py` (imports likely already present; add what's missing):

```python
def test_finalize_persists_log_tail_for_success():
    from app.services.workloads.run_service import _finalize_workload_run, _LOG_TAIL_BYTES
    from app.models.workload_run import WorkloadRun
    from tests.conftest import TestSession
    import uuid

    db = TestSession()
    run = WorkloadRun(id=str(uuid.uuid4()), kind="ad_hoc", status="running")
    db.add(run)
    db.commit()
    run_id = run.id
    db.close()

    big = "x" * (_LOG_TAIL_BYTES + 5000)
    _finalize_workload_run(run_id, "succeeded", big)

    db = TestSession()
    saved = db.get(WorkloadRun, run_id)
    assert saved.status == "succeeded"
    assert saved.log_ref is not None
    assert len(saved.log_ref) == _LOG_TAIL_BYTES
    assert saved.log_ref == big[-_LOG_TAIL_BYTES:]
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python3 -m pytest tests/test_workload_run_service.py::test_finalize_persists_log_tail_for_success -v`
Expected: FAIL — `ImportError` on `_LOG_TAIL_BYTES` (or `log_ref is None`).

- [ ] **Step 3: Write minimal implementation**

Near the top of `run_service.py` (with the other module constants), add:

```python
_LOG_TAIL_BYTES = 256 * 1024
```

In `_finalize_workload_run`, inside the `if run is not None:` block, after `run.ended_at = _now()`:

```python
            run.log_ref = (error_or_logs or "")[-_LOG_TAIL_BYTES:]
```

(Leave the existing `run.error = error_or_logs[:2000]` for error/timeout unchanged.)

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/python3 -m pytest tests/test_workload_run_service.py::test_finalize_persists_log_tail_for_success -v`
Expected: PASS

- [ ] **Step 5: Format, typecheck, commit**

```bash
cd /Users/prutledg/troshka/src/backend && black app/services/workloads/run_service.py tests/test_workload_run_service.py && ./venv/bin/pyright app/services/workloads/run_service.py
cd /Users/prutledg/troshka && git add src/backend/app/services/workloads/run_service.py src/backend/tests/test_workload_run_service.py && git commit -m "feat(workloads): persist bounded run-log tail to log_ref on finalize"
```

---

### Task 2: `GET /workloads/{run_id}/log` endpoint

**Files:**
- Modify: `src/backend/app/services/workloads/run_service.py` (add `get_workload_log`, `_TERMINAL_STATUSES`)
- Modify: `src/backend/app/api/workloads.py` (add response model + route)
- Test: `src/backend/tests/test_workload_run_service.py`, `src/backend/tests/test_workload_api.py`

**Interfaces:**
- Consumes: `_read_runner_pod_logs(host, run_id)`, `_host_for_project(db, project)` (both in `run_service`), `WorkloadRun.log_ref` (Task 1).
- Produces: `get_workload_log(db, run) -> str` — persisted log for terminal/orphan runs; live pod read (falling back to `log_ref`) otherwise. Route `GET /workloads/{run_id}/log` → `WorkloadLogResponse{id, status, log}`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_workload_run_service.py`:

```python
def test_get_workload_log_terminal_returns_persisted():
    from app.services.workloads.run_service import get_workload_log
    from app.models.workload_run import WorkloadRun
    from tests.conftest import TestSession
    import uuid

    db = TestSession()
    run = WorkloadRun(id=str(uuid.uuid4()), kind="ad_hoc", status="succeeded", log_ref="persisted log")
    assert get_workload_log(db, run) == "persisted log"
    db.close()


def test_get_workload_log_running_reads_live(monkeypatch):
    from app.services.workloads import run_service
    from app.models.workload_run import WorkloadRun
    from app.models.project import Project
    from tests.conftest import TestSession
    import uuid

    db = TestSession()
    proj = Project(id=str(uuid.uuid4()), name="p", state="active", host_id="h1", topology={})
    db.add(proj)
    db.commit()
    run = WorkloadRun(id=str(uuid.uuid4()), kind="ad_hoc", status="running",
                      project_id=proj.id, log_ref="stale")

    monkeypatch.setattr(run_service, "_host_for_project", lambda _db, _p: object())
    monkeypatch.setattr(run_service, "_read_runner_pod_logs", lambda _h, _rid: "LIVE OUTPUT")
    assert run_service.get_workload_log(db, run) == "LIVE OUTPUT"
    db.close()


def test_get_workload_log_running_falls_back_on_error(monkeypatch):
    from app.services.workloads import run_service
    from app.models.workload_run import WorkloadRun
    from app.models.project import Project
    from tests.conftest import TestSession
    import uuid

    db = TestSession()
    proj = Project(id=str(uuid.uuid4()), name="p", state="active", host_id="h1", topology={})
    db.add(proj)
    db.commit()
    run = WorkloadRun(id=str(uuid.uuid4()), kind="ad_hoc", status="running",
                      project_id=proj.id, log_ref="fallback log")

    def _boom(_db, _p):
        raise RuntimeError("no host")

    monkeypatch.setattr(run_service, "_host_for_project", _boom)
    assert run_service.get_workload_log(db, run) == "fallback log"
    db.close()
```

In `tests/test_workload_api.py`:

```python
def test_get_run_log_endpoint_terminal():
    pid = _create_project()
    rid = _create_workload_run(pid, kind="ad_hoc", status="succeeded")
    db = TestSession()
    run = db.get(WorkloadRun, rid)
    run.log_ref = "hello from the pod"
    db.commit()
    db.close()

    r = client.get(f"/api/v1/workloads/{rid}/log")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == rid
    assert body["status"] == "succeeded"
    assert body["log"] == "hello from the pod"


def test_get_run_log_endpoint_404():
    r = client.get("/api/v1/workloads/00000000-0000-0000-0000-000000000000/log")
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/python3 -m pytest tests/test_workload_run_service.py -k get_workload_log tests/test_workload_api.py -k run_log -v`
Expected: FAIL — `get_workload_log` undefined / route 404 for the terminal case.

- [ ] **Step 3: Write minimal implementation**

In `run_service.py` add (near the other helpers):

```python
_TERMINAL_STATUSES = {"succeeded", "error", "timeout"}


def get_workload_log(db, run) -> str:
    """Return the runner log: persisted tail for terminal/orphan runs, live otherwise."""
    if run.status in _TERMINAL_STATUSES or not run.project_id:
        return run.log_ref or ""
    try:
        project = db.get(Project, run.project_id)
        if not project or not project.host_id:
            return run.log_ref or ""
        host = _host_for_project(db, project)
        return _read_runner_pod_logs(host, run.id) or run.log_ref or ""
    except Exception:  # noqa: BLE001 — transient pod-read failure → persisted tail
        return run.log_ref or ""
```

In `app/api/workloads.py` add the response model (next to the others):

```python
class WorkloadLogResponse(BaseModel):
    id: str
    status: str
    log: str
```

And the route (after `get_workload_run_status`):

```python
@router.get(
    "/workloads/{run_id}/log",
    response_model=WorkloadLogResponse,
    responses={403: {}, 404: {}},
)
def get_workload_run_log(run_id: str, user: CurrentUser, db: DbSession):
    run = db.get(WorkloadRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=_RUN_NOT_FOUND)
    if run.project_id:
        project = db.get(Project, run.project_id)
        assert project is not None, f"Project {run.project_id} not found"
        _enforce_project_access(project, user)
    elif user.role != "admin":
        raise HTTPException(status_code=403, detail=_ACCESS_DENIED)

    from app.services.workloads.run_service import get_workload_log

    return WorkloadLogResponse(id=run.id, status=run.status, log=get_workload_log(db, run))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python3 -m pytest tests/test_workload_run_service.py -k get_workload_log tests/test_workload_api.py -k run_log -v`
Expected: PASS

- [ ] **Step 5: Format, typecheck, commit**

```bash
cd /Users/prutledg/troshka/src/backend && black app/services/workloads/run_service.py app/api/workloads.py tests/test_workload_run_service.py tests/test_workload_api.py && ./venv/bin/pyright app/services/workloads/run_service.py app/api/workloads.py
cd /Users/prutledg/troshka && git add src/backend/app/services/workloads/run_service.py src/backend/app/api/workloads.py src/backend/tests/test_workload_run_service.py src/backend/tests/test_workload_api.py && git commit -m "feat(workloads): GET /workloads/{id}/log (live read + persisted fallback)"
```

---

### Task 3: Inventory preview helper + endpoint

**Files:**
- Modify: `src/backend/app/services/workloads/inventory.py` (add `preview_inventory`)
- Modify: `src/backend/app/api/workloads.py` (add request/response models + route)
- Test: `src/backend/tests/test_workload_inventory.py`, `src/backend/tests/test_workload_api.py`

**Interfaces:**
- Consumes: existing `_vm_nodes`, `_groups`, `validate_ansible_groups`, `InventoryError` (in `inventory.py`); `_has_ocp` (in `run_service`).
- Produces: `preview_inventory(topology) -> dict[str, list[str]]` (group → VM names, pure, no validation). Route `POST /projects/{id}/workloads/inventory-preview` → `InventoryPreviewResponse{groups, errors}`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_workload_inventory.py`:

```python
def test_preview_inventory_maps_groups_to_names():
    from app.services.workloads.inventory import preview_inventory

    topo = {
        "nodes": [
            {"id": "n1", "type": "vmNode",
             "data": {"name": "bastion", "tags": {"AnsibleGroup": "bastions, all"}}},
            {"id": "n2", "type": "vmNode",
             "data": {"name": "web1", "tags": {"AnsibleGroup": "web"}}},
            {"id": "n3", "type": "networkNode", "data": {"name": "net"}},
        ]
    }
    groups = preview_inventory(topo)
    assert groups["bastions"] == ["bastion"]
    assert groups["all"] == ["bastion"]
    assert groups["web"] == ["web1"]
    assert "net" not in {v for vs in groups.values() for v in vs}


def test_preview_inventory_empty():
    from app.services.workloads.inventory import preview_inventory
    assert preview_inventory({"nodes": []}) == {}
```

In `tests/test_workload_api.py`:

```python
def test_inventory_preview_reports_contract_errors():
    # VM with no AnsibleGroup tag → validation error surfaced, groups empty
    topo = {"nodes": [{"id": "n1", "type": "vmNode",
                       "data": {"name": "vm1", "nics": [{"ip": "10.0.0.5"}]}}]}
    pid = _create_project(topology=topo)
    r = client.post(f"/api/v1/projects/{pid}/workloads/inventory-preview", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["groups"] == {}
    assert any("AnsibleGroup" in e for e in body["errors"])


def test_inventory_preview_valid_ssh_topology():
    topo = {
        "nodes": [{"id": "n1", "type": "vmNode",
                   "data": {"name": "bastion", "nics": [{"ip": "10.0.0.5"}],
                            "tags": {"AnsibleGroup": "bastions"}}}],
        "externalIps": [{"vmId": "n1", "ip": "1.2.3.4"}],
    }
    pid = _create_project(topology=topo)
    r = client.post(f"/api/v1/projects/{pid}/workloads/inventory-preview", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["errors"] == []
    assert body["groups"]["bastions"] == ["bastion"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./venv/bin/python3 -m pytest tests/test_workload_inventory.py -k preview tests/test_workload_api.py -k inventory_preview -v`
Expected: FAIL — `preview_inventory` undefined / route 404.

- [ ] **Step 3: Write minimal implementation**

In `inventory.py` add:

```python
def preview_inventory(topology: dict) -> dict[str, list[str]]:
    """PURE: map each AnsibleGroup tag value to the VM names in it (no validation)."""
    groups: dict[str, list[str]] = {}
    for node in _vm_nodes(topology):
        name = (node.get("data") or {}).get("name") or node.get("id") or ""
        for group in _groups(node):
            groups.setdefault(group, []).append(name)
    return groups
```

In `app/api/workloads.py` add models:

```python
class InventoryPreviewRequest(BaseModel):
    target_map: dict | None = None


class InventoryPreviewResponse(BaseModel):
    groups: dict[str, list[str]]
    errors: list[str]
```

And the route:

```python
@router.post(
    "/projects/{project_id}/workloads/inventory-preview",
    response_model=InventoryPreviewResponse,
    responses={403: {}, 404: {}},
)
def preview_workload_inventory(
    project_id: str,
    body: InventoryPreviewRequest,  # noqa: ARG001 — reserved for future scoping
    user: CurrentUser,
    db: DbSession,
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=_PROJECT_NOT_FOUND)
    _enforce_project_access(project, user)

    from app.services.workloads.inventory import (
        InventoryError,
        preview_inventory,
        validate_ansible_groups,
    )

    topo = project.deployed_topology or project.topology or {}
    groups = preview_inventory(topo)
    errors: list[str] = []
    try:
        validate_ansible_groups(topo, require_bastion=(not _has_ocp(project)))
    except InventoryError as exc:
        errors.append(str(exc))
    return InventoryPreviewResponse(groups=groups, errors=errors)
```

(`_has_ocp` is already imported in `workloads.py` from `run_service`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `./venv/bin/python3 -m pytest tests/test_workload_inventory.py -k preview tests/test_workload_api.py -k inventory_preview -v`
Expected: PASS

- [ ] **Step 5: Format, typecheck, commit**

```bash
cd /Users/prutledg/troshka/src/backend && black app/services/workloads/inventory.py app/api/workloads.py tests/test_workload_inventory.py tests/test_workload_api.py && ./venv/bin/pyright app/services/workloads/inventory.py app/api/workloads.py
cd /Users/prutledg/troshka && git add src/backend/app/services/workloads/inventory.py src/backend/app/api/workloads.py src/backend/tests/test_workload_inventory.py src/backend/tests/test_workload_api.py && git commit -m "feat(workloads): inventory-preview endpoint (groups + contract errors)"
```

---

### Task 4: `target_map` mode gates inventory validation

**Files:**
- Modify: `src/backend/app/services/workloads/run_service.py` (`run_workload_job` ~97-98; add `_should_validate_inventory`)
- Test: `src/backend/tests/test_workload_run_service.py`

**Interfaces:**
- Produces: `_should_validate_inventory(target_map) -> bool` — `False` only when `target_map["mode"] == "cluster"`; `True` otherwise (incl. `None`). `run_workload_job` calls `validate_ansible_groups` only when this returns `True`.

- [ ] **Step 1: Write the failing test**

In `tests/test_workload_run_service.py`:

```python
def test_should_validate_inventory_modes():
    from app.services.workloads.run_service import _should_validate_inventory
    assert _should_validate_inventory(None) is True            # legacy default
    assert _should_validate_inventory({}) is True
    assert _should_validate_inventory({"mode": "vms"}) is True
    assert _should_validate_inventory({"mode": "cluster"}) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./venv/bin/python3 -m pytest tests/test_workload_run_service.py::test_should_validate_inventory_modes -v`
Expected: FAIL — `_should_validate_inventory` undefined.

- [ ] **Step 3: Write minimal implementation**

In `run_service.py` add:

```python
def _should_validate_inventory(target_map) -> bool:
    """Skip the whole-project AnsibleGroup contract only for cluster-only runs."""
    return (target_map or {}).get("mode") != "cluster"
```

In `run_workload_job`, replace the unconditional validation line:

```python
        validate_ansible_groups(topo, require_bastion=(not _has_ocp(project)))
```

with:

```python
        if _should_validate_inventory(run.target_map):
            validate_ansible_groups(topo, require_bastion=(not _has_ocp(project)))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./venv/bin/python3 -m pytest tests/test_workload_run_service.py::test_should_validate_inventory_modes -v`
Expected: PASS

- [ ] **Step 5: Full backend suite, format, typecheck, commit**

```bash
cd /Users/prutledg/troshka/src/backend && black app/services/workloads/run_service.py tests/test_workload_run_service.py && ./venv/bin/pyright app/services/workloads/run_service.py && ./venv/bin/python3 -m pytest tests/ -q
cd /Users/prutledg/troshka && git add src/backend/app/services/workloads/run_service.py src/backend/tests/test_workload_run_service.py && git commit -m "feat(workloads): target_map cluster mode skips VM inventory validation"
```

Expected: full suite green.

---

### Task 5: Canvas first-class "Ansible Groups" field

**Files:**
- Modify: `src/frontend/src/components/canvas/PropertiesPanel.tsx` (insert a section in the `nodeType === "vmNode"` block, immediately BEFORE the existing Tags section ~line 2274)
- Test: `src/frontend/src/components/canvas/__tests__/PropertiesPanelAnsibleGroups.test.tsx`

**Interfaces:**
- Consumes: existing `data` (`node.data`), `update(field, value)` helper (writes via `updateNodeData`). No new store API.

- [ ] **Step 1: Write the failing test**

Create `src/frontend/src/components/canvas/__tests__/PropertiesPanelAnsibleGroups.test.tsx`:

```tsx
import { describe, it, expect, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { useCanvasStore } from "@/stores/canvasStore";
import PropertiesPanel from "@/components/canvas/PropertiesPanel";

function seedStore(nodeData: Record<string, unknown>) {
  useCanvasStore.setState({
    nodes: [{ id: "vm-1", type: "vmNode", position: { x: 0, y: 0 }, data: nodeData }],
    edges: [],
    selectedNodeId: "vm-1",
    projectState: "draft",
    clusters: [],
    deployedNodeData: {},
    deployedEdgeKey: "",
    deployedExternalIps: "[]",
    deployedClusters: "[]",
    externalIps: [],
  } as never);
}

describe("PropertiesPanel Ansible Groups field", () => {
  beforeEach(() => seedStore({ name: "vm-1", os: "rhel9" }));

  it("writes AnsibleGroup into node data.tags", () => {
    render(<PropertiesPanel />);
    const input = screen.getByPlaceholderText("e.g. bastions, webservers") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "bastions, web" } });
    const node = useCanvasStore.getState().nodes.find((n) => n.id === "vm-1")!;
    expect(((node.data as Record<string, any>).tags || {}).AnsibleGroup).toBe("bastions, web");
  });

  it("shows existing AnsibleGroup value", () => {
    seedStore({ name: "vm-1", os: "rhel9", tags: { AnsibleGroup: "db" } });
    render(<PropertiesPanel />);
    const input = screen.getByPlaceholderText("e.g. bastions, webservers") as HTMLInputElement;
    expect(input.value).toBe("db");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/prutledg/troshka/src/frontend && npm test -- src/components/canvas/__tests__/PropertiesPanelAnsibleGroups.test.tsx`
Expected: FAIL — placeholder not found.

- [ ] **Step 3: Write minimal implementation**

In `PropertiesPanel.tsx`, inside the `{nodeType === "vmNode" && (` block, immediately before the `{/* Tags Section */}` comment (~line 2274), insert:

```tsx
          {/* Ansible Groups (workloads inventory targeting) */}
          <div className="props-section">
            <div className="props-section-title">Ansible Groups</div>
            <div className="props-section-body">
              <input
                className="props-input"
                value={((data.tags || {}).AnsibleGroup as string) || ""}
                onChange={(e) =>
                  update("tags", { ...(data.tags || {}), AnsibleGroup: e.target.value })
                }
                placeholder="e.g. bastions, webservers"
                style={{ width: "100%", fontSize: 11 }}
              />
              <div style={{ fontSize: 10, opacity: 0.6, marginTop: 4 }}>
                Comma-separated. VM-targeted runs need a name + first-NIC IP; SSH mode
                needs exactly one “bastions” VM with an external IP.
              </div>
            </div>
          </div>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/prutledg/troshka/src/frontend && npm test -- src/components/canvas/__tests__/PropertiesPanelAnsibleGroups.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/components/canvas/PropertiesPanel.tsx src/frontend/src/components/canvas/__tests__/PropertiesPanelAnsibleGroups.test.tsx && git commit -m "feat(workloads): first-class Ansible Groups field on VM nodes"
```

---

### Task 6: `workload-progress` in useVmStateSocket

**Files:**
- Modify: `src/frontend/src/hooks/useVmStateSocket.ts` (interface ~10-14/29, state ~51, `onmessage` switch ~94-148, return ~180)
- Test: `src/frontend/src/hooks/__tests__/useVmStateSocketWorkload.test.tsx`

**Interfaces:**
- Produces: hook returns `workloadProgress: WorkloadProgress | null` where `interface WorkloadProgress { run_id?: string; step: string; detail: string }`, set from a `case "workload-progress":` mirroring `deploy-progress`.

- [ ] **Step 1: Write the failing test**

Create `src/frontend/src/hooks/__tests__/useVmStateSocketWorkload.test.tsx`. Mock the global `WebSocket`, render a probe component, dispatch a `workload-progress` message, assert the returned value:

```tsx
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, act, waitFor } from "@testing-library/react";
import { useVmStateSocket } from "@/hooks/useVmStateSocket";

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  onopen: ((e: unknown) => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: unknown) => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;
  readyState = 1;
  url: string;
  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
    setTimeout(() => this.onopen?.({}), 0);
  }
  send() {}
  close() {
    this.readyState = 3;
    this.onclose?.({});
  }
}

function Probe({ projectId }: { projectId: string }) {
  const ws = useVmStateSocket(projectId);
  return <div data-testid="wp">{ws.workloadProgress?.step ?? "none"}</div>;
}

describe("useVmStateSocket workload-progress", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket as unknown as typeof WebSocket);
    // dev mode skips ws-token fetch; stub fetch defensively
    vi.stubGlobal("fetch", vi.fn(() =>
      Promise.resolve({ ok: true, json: () => Promise.resolve({ token: "x" }) } as Response),
    ));
  });
  afterEach(() => vi.unstubAllGlobals());

  it("surfaces workload-progress step", async () => {
    render(<Probe projectId="p1" />);
    await waitFor(() => expect(MockWebSocket.instances.length).toBeGreaterThan(0));
    const sock = MockWebSocket.instances[0];
    act(() => {
      sock.onmessage?.({
        data: JSON.stringify({ type: "workload-progress", progress: { step: "Install role", detail: "" } }),
      });
    });
    await waitFor(() => expect(screen.getByTestId("wp").textContent).toBe("Install role"));
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/prutledg/troshka/src/frontend && npm test -- src/hooks/__tests__/useVmStateSocketWorkload.test.tsx`
Expected: FAIL — `ws.workloadProgress` is `undefined`.

> Executor note: read the actual hook first to match its real dev-mode detection and WS-URL/token logic; adapt the mock if the hook fetches a token before constructing the socket. The four edits below are additive.

- [ ] **Step 3: Write minimal implementation**

Add near the `DeployProgress` interface (~10-14):

```ts
interface WorkloadProgress {
  run_id?: string;
  step: string;
  detail: string;
}
```

Add the field to the hook's return-type interface (~line 29):

```ts
  workloadProgress: WorkloadProgress | null;
```

Add state (near `deployProgress`, ~51):

```ts
  const [workloadProgress, setWorkloadProgress] = useState<WorkloadProgress | null>(null);
```

Add a case in the `switch (msg.type)` (next to `deploy-progress`):

```ts
          case "workload-progress": {
            const wp = msg.progress || msg;
            setWorkloadProgress({ run_id: wp.run_id, step: wp.step || "", detail: wp.detail || "" });
            break;
          }
```

Append `workloadProgress` to the return object (~line 180):

```ts
  return { connected, vmStates, vmProgress, vmBootDevs, projectState, deployError, deployProgress, workloadProgress, ocpHealth, topologyUpdate, externalIpsUpdate, deleted, timerWarning, timerFired, autoStopExpiresAt, lifetimeExpiresAt, autoStopped };
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/prutledg/troshka/src/frontend && npm test -- src/hooks/__tests__/useVmStateSocketWorkload.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/hooks/useVmStateSocket.ts src/frontend/src/hooks/__tests__/useVmStateSocketWorkload.test.tsx && git commit -m "feat(workloads): surface workload-progress on the project socket"
```

---

### Task 7: RunWorkloadModal (ad-hoc trigger form) + action-bar button

**Files:**
- Create: `src/frontend/src/components/canvas/RunWorkloadModal.tsx`
- Modify: `src/frontend/src/app/projects/[id]/page.tsx` (import; `showWorkloadModal` state; button in `.project-action-bar-right`; conditional render)
- Test: `src/frontend/src/components/canvas/__tests__/RunWorkloadModal.test.tsx`

**Interfaces:**
- Produces: `export default function RunWorkloadModal({ projectId, onClose, onLaunched }: { projectId: string; onClose: () => void; onLaunched: (runId: string) => void })`.
- Behavior: fields `role_fqcn` (required), repeatable requirements rows (`name`/`type`/`version`), optional `ee_image`, target mode radio (`cluster` | `vms`). In `vms` mode it `POST`s `/api/v1/projects/{id}/workloads/inventory-preview` and renders `groups` + `errors`, disabling Launch while `errors.length > 0`. Launch `POST`s `/api/v1/projects/{id}/workloads` with `{ kind: "ad_hoc", role_fqcn, requirements_content: { collections }, ee_image?, target_map }`; on 202 calls `onLaunched(body.id)`; on non-2xx shows the response `detail` inline.

- [ ] **Step 1: Write the failing test**

Create `src/frontend/src/components/canvas/__tests__/RunWorkloadModal.test.tsx`:

```tsx
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import RunWorkloadModal from "@/components/canvas/RunWorkloadModal";

function okJson(data: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(data),
  } as Response);
}

describe("RunWorkloadModal", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  it("launches an ad-hoc cluster run and calls onLaunched with the run id", async () => {
    let postBody: any = null;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.endsWith("/workloads") && init?.method === "POST") {
          postBody = JSON.parse(init!.body as string);
          return okJson({ id: "run-123", status: "pending" }, 202);
        }
        return okJson({});
      }),
    );
    const onLaunched = vi.fn();
    render(<RunWorkloadModal projectId="p1" onClose={() => {}} onLaunched={onLaunched} />);

    fireEvent.change(screen.getByPlaceholderText(/role/i), {
      target: { value: "agnosticd.core_workloads.ocp4_workload_example" },
    });
    fireEvent.click(screen.getByRole("button", { name: /launch/i }));

    await waitFor(() => expect(onLaunched).toHaveBeenCalledWith("run-123"));
    expect(postBody.kind).toBe("ad_hoc");
    expect(postBody.role_fqcn).toBe("agnosticd.core_workloads.ocp4_workload_example");
    expect(postBody.target_map.mode).toBe("cluster");
  });

  it("disables Launch when VM-mode preview reports contract errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = typeof input === "string" ? input : input.toString();
        if (url.endsWith("/inventory-preview")) {
          return okJson({ groups: {}, errors: ["VM vm1 has no AnsibleGroup tag"] });
        }
        return okJson({});
      }),
    );
    render(<RunWorkloadModal projectId="p1" onClose={() => {}} onLaunched={() => {}} />);
    fireEvent.change(screen.getByPlaceholderText(/role/i), { target: { value: "some.role" } });
    fireEvent.click(screen.getByLabelText(/vms/i));
    await waitFor(() => expect(screen.getByText(/no AnsibleGroup tag/i)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /launch/i })).toBeDisabled();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/prutledg/troshka/src/frontend && npm test -- src/components/canvas/__tests__/RunWorkloadModal.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

Create `src/frontend/src/components/canvas/RunWorkloadModal.tsx` (hand-rolled overlay per convention):

```tsx
"use client";

import React, { useEffect, useState } from "react";

interface ReqRow {
  name: string;
  type: string;
  version: string;
}
interface Props {
  projectId: string;
  onClose: () => void;
  onLaunched: (runId: string) => void;
}

export default function RunWorkloadModal({ projectId, onClose, onLaunched }: Props) {
  const [roleFqcn, setRoleFqcn] = useState("");
  const [eeImage, setEeImage] = useState("");
  const [rows, setRows] = useState<ReqRow[]>([]);
  const [mode, setMode] = useState<"cluster" | "vms">("cluster");
  const [groups, setGroups] = useState<Record<string, string[]>>({});
  const [previewErrors, setPreviewErrors] = useState<string[]>([]);
  const [launching, setLaunching] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (mode !== "vms") return;
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`/api/v1/projects/${projectId}/workloads/inventory-preview`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ target_map: { mode: "vms" } }),
        });
        const data = await r.json();
        if (!cancelled) {
          setGroups(data.groups || {});
          setPreviewErrors(data.errors || []);
        }
      } catch {
        if (!cancelled) setPreviewErrors(["Failed to load inventory preview"]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [mode, projectId]);

  const launchDisabled =
    launching || !roleFqcn.trim() || (mode === "vms" && previewErrors.length > 0);

  const launch = async () => {
    setLaunching(true);
    setError("");
    const collections = rows
      .filter((r) => r.name.trim())
      .map((r) => ({ name: r.name.trim(), type: r.type || "git", version: r.version || "main" }));
    const body: Record<string, unknown> = {
      kind: "ad_hoc",
      role_fqcn: roleFqcn.trim(),
      target_map: { mode, ...(mode === "vms" ? { vm_groups: Object.keys(groups) } : {}) },
    };
    if (collections.length) body.requirements_content = { collections };
    if (eeImage.trim()) body.ee_image = eeImage.trim();
    try {
      const r = await fetch(`/api/v1/projects/${projectId}/workloads`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (r.status === 202) {
        const data = await r.json();
        onLaunched(data.id);
        return;
      }
      const data = await r.json().catch(() => ({}));
      setError(data.detail || `Launch failed (${r.status})`);
    } catch {
      setError("Launch failed");
    } finally {
      setLaunching(false);
    }
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 10000,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "rgba(0,0,0,0.6)",
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget && !launching) onClose();
      }}
    >
      <div
        style={{
          background: "var(--pf-t--global--background--color--primary--default)",
          borderRadius: 12,
          padding: 24,
          width: 560,
          maxWidth: "92vw",
          maxHeight: "85vh",
          overflowY: "auto",
          boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
          border: "1px solid var(--pf-t--global--border--color--default)",
        }}
      >
        <h2 style={{ marginTop: 0, marginBottom: 16 }}>Run Workload</h2>

        <label style={{ fontSize: 12, opacity: 0.8 }}>Role FQCN</label>
        <input
          className="props-input"
          value={roleFqcn}
          onChange={(e) => setRoleFqcn(e.target.value)}
          placeholder="role FQCN, e.g. agnosticd.core_workloads.ocp4_workload_example"
          style={{ width: "100%", marginBottom: 12 }}
        />

        <label style={{ fontSize: 12, opacity: 0.8 }}>EE image (optional)</label>
        <input
          className="props-input"
          value={eeImage}
          onChange={(e) => setEeImage(e.target.value)}
          placeholder="defaults to server config"
          style={{ width: "100%", marginBottom: 12 }}
        />

        <div style={{ fontSize: 12, opacity: 0.8, marginBottom: 4 }}>Requirements (git collections)</div>
        {rows.map((row, i) => (
          <div key={i} style={{ display: "flex", gap: 4, marginBottom: 4 }}>
            <input
              className="props-input"
              placeholder="git URL"
              value={row.name}
              onChange={(e) =>
                setRows((rs) => rs.map((r, j) => (j === i ? { ...r, name: e.target.value } : r)))
              }
              style={{ flex: 3 }}
            />
            <input
              className="props-input"
              placeholder="version"
              value={row.version}
              onChange={(e) =>
                setRows((rs) => rs.map((r, j) => (j === i ? { ...r, version: e.target.value } : r)))
              }
              style={{ flex: 1 }}
            />
            <button onClick={() => setRows((rs) => rs.filter((_, j) => j !== i))}>✕</button>
          </div>
        ))}
        <button
          className="props-library-btn"
          onClick={() => setRows((rs) => [...rs, { name: "", type: "git", version: "main" }])}
          style={{ padding: "4px 8px", fontSize: 11, marginBottom: 12 }}
        >
          + Add requirement
        </button>

        <div style={{ marginBottom: 12 }}>
          <label style={{ marginRight: 16 }}>
            <input
              type="radio"
              name="wl-target"
              checked={mode === "cluster"}
              onChange={() => setMode("cluster")}
            />{" "}
            Cluster
          </label>
          <label>
            <input
              type="radio"
              name="wl-target"
              aria-label="VMs"
              checked={mode === "vms"}
              onChange={() => setMode("vms")}
            />{" "}
            VMs
          </label>
        </div>

        {mode === "vms" && (
          <div style={{ marginBottom: 12, fontSize: 12 }}>
            {previewErrors.length > 0 ? (
              previewErrors.map((e, i) => (
                <div key={i} style={{ color: "var(--pf-t--global--color--status--danger--default)" }}>
                  {e}
                </div>
              ))
            ) : (
              <div style={{ opacity: 0.8 }}>
                {Object.entries(groups).map(([g, hosts]) => (
                  <div key={g}>
                    <strong>{g}</strong>: {hosts.join(", ")}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {error && (
          <div style={{ color: "var(--pf-t--global--color--status--danger--default)", marginBottom: 12 }}>
            {error}
          </div>
        )}

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button onClick={onClose} disabled={launching}>
            Cancel
          </button>
          <button className="project-publish-btn" onClick={launch} disabled={launchDisabled}>
            {launching ? "Launching…" : "Launch"}
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/prutledg/troshka/src/frontend && npm test -- src/components/canvas/__tests__/RunWorkloadModal.test.tsx`
Expected: PASS

- [ ] **Step 5: Wire the action-bar button (no test — manual/covered by Task 9 integration)**

In `src/frontend/src/app/projects/[id]/page.tsx`:

Add import near `SavePatternModal` (~line 13): `import RunWorkloadModal from "@/components/canvas/RunWorkloadModal";`

Add state near `showPatternModal` (~line 31): `const [showWorkloadModal, setShowWorkloadModal] = useState(false);` and `const [openRunId, setOpenRunId] = useState<string | null>(null);`

Add a button in `.project-action-bar-right` after the "Save as Pattern" button, gated on active state:

```tsx
          {projectState === "active" && (
            <button
              className="project-publish-btn"
              onClick={() => setShowWorkloadModal(true)}
              style={{ opacity: 0.85 }}
            >
              Run Workload
            </button>
          )}
```

Add the conditional render near the `SavePatternModal` render (~line 1073):

```tsx
      {showWorkloadModal && (
        <RunWorkloadModal
          projectId={projectId}
          onClose={() => setShowWorkloadModal(false)}
          onLaunched={(runId) => {
            setShowWorkloadModal(false);
            setOpenRunId(runId);
          }}
        />
      )}
```

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/components/canvas/RunWorkloadModal.tsx src/frontend/src/components/canvas/__tests__/RunWorkloadModal.test.tsx src/frontend/src/app/projects/\[id\]/page.tsx && git commit -m "feat(workloads): Run Workload ad-hoc trigger form + action-bar button"
```

---

### Task 8: WorkloadRunDetailModal (live progress + logs)

**Files:**
- Create: `src/frontend/src/components/canvas/WorkloadRunDetailModal.tsx`
- Modify: `src/frontend/src/app/projects/[id]/page.tsx` (render when `openRunId` set; pass `ws.workloadProgress` as nudge)
- Test: `src/frontend/src/components/canvas/__tests__/WorkloadRunDetailModal.test.tsx`

**Interfaces:**
- Consumes: `GET /api/v1/workloads/{runId}/log` → `{ id, status, log }`; optional `wsNudge` prop (any changing value from `ws.workloadProgress`) triggers an immediate refetch.
- Produces: `export default function WorkloadRunDetailModal({ runId, onClose, wsNudge }: { runId: string; onClose: () => void; wsNudge?: unknown })`.
- Behavior: polls the log endpoint every 3s into an auto-scrolling `<pre>`; header shows the status; stops polling on terminal status (`succeeded|error|timeout`); refetches immediately when `wsNudge` changes.

- [ ] **Step 1: Write the failing test**

Create `src/frontend/src/components/canvas/__tests__/WorkloadRunDetailModal.test.tsx`:

```tsx
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import WorkloadRunDetailModal from "@/components/canvas/WorkloadRunDetailModal";

describe("WorkloadRunDetailModal", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  it("fetches and renders the run log + status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve({ id: "run-1", status: "succeeded", log: "PLAY RECAP\nok=5" }),
        } as Response),
      ),
    );
    render(<WorkloadRunDetailModal runId="run-1" onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText(/PLAY RECAP/)).toBeInTheDocument());
    expect(screen.getByText(/succeeded/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/prutledg/troshka/src/frontend && npm test -- src/components/canvas/__tests__/WorkloadRunDetailModal.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

Create `src/frontend/src/components/canvas/WorkloadRunDetailModal.tsx` (mirrors `ClusterInstallLogModal` polling + auto-scroll):

```tsx
"use client";

import React, { useEffect, useRef, useState } from "react";

interface Props {
  runId: string;
  onClose: () => void;
  wsNudge?: unknown;
}

const TERMINAL = new Set(["succeeded", "error", "timeout"]);

export default function WorkloadRunDetailModal({ runId, onClose, wsNudge }: Props) {
  const [log, setLog] = useState("");
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);
  const preRef = useRef<HTMLPreElement>(null);
  const statusRef = useRef("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const fetchLog = async () => {
      try {
        const r = await fetch(`/api/v1/workloads/${runId}/log`);
        if (!r.ok || cancelled) return;
        const data = await r.json();
        if (!cancelled) {
          setLog(data.log || "");
          setStatus(data.status || "");
          statusRef.current = data.status || "";
        }
      } catch {
        /* transient — keep last log */
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    fetchLog();
    const timer = setInterval(() => {
      if (TERMINAL.has(statusRef.current)) {
        clearInterval(timer);
        return;
      }
      fetchLog();
    }, 3000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [runId, wsNudge]);

  useEffect(() => {
    if (preRef.current) preRef.current.scrollTop = preRef.current.scrollHeight;
  }, [log]);

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 10000,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "rgba(0,0,0,0.6)",
      }}
      onClick={onClose}
    >
      <div
        style={{
          background: "var(--pf-t--global--background--color--primary--default)",
          borderRadius: 12,
          padding: 24,
          width: "80vw",
          maxWidth: 900,
          maxHeight: "80vh",
          boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
          border: "1px solid var(--pf-t--global--border--color--default)",
          display: "flex",
          flexDirection: "column",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>Workload Run — {status || "pending"}</h2>
          <button onClick={onClose}>✕</button>
        </div>
        <pre
          ref={preRef}
          style={{
            fontSize: 11,
            fontFamily: "monospace",
            whiteSpace: "pre-wrap",
            overflowY: "auto",
            flex: 1,
            margin: 0,
            padding: 8,
            background: "rgba(0,0,0,0.2)",
            borderRadius: 6,
            lineHeight: 1.5,
          }}
        >
          {log || <span style={{ opacity: 0.5 }}>{loading ? "Loading run log…" : "No log yet."}</span>}
        </pre>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/prutledg/troshka/src/frontend && npm test -- src/components/canvas/__tests__/WorkloadRunDetailModal.test.tsx`
Expected: PASS

- [ ] **Step 5: Wire into the page**

In `page.tsx`, where the socket hook is consumed (it already returns `ws`), render the detail modal when `openRunId` is set, passing the WS nudge:

```tsx
      {openRunId && (
        <WorkloadRunDetailModal
          runId={openRunId}
          wsNudge={ws.workloadProgress}
          onClose={() => setOpenRunId(null)}
        />
      )}
```

Add the import near the others: `import WorkloadRunDetailModal from "@/components/canvas/WorkloadRunDetailModal";`
(If the hook result isn't already bound to a local named `ws`, use the existing variable name; only `ws.workloadProgress` is needed.)

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/components/canvas/WorkloadRunDetailModal.tsx src/frontend/src/components/canvas/__tests__/WorkloadRunDetailModal.test.tsx src/frontend/src/app/projects/\[id\]/page.tsx && git commit -m "feat(workloads): run-detail modal with polled logs + WS nudge"
```

---

### Task 9: WorkloadRunsModal (run history) + launcher

**Files:**
- Create: `src/frontend/src/components/canvas/WorkloadRunsModal.tsx`
- Modify: `src/frontend/src/app/projects/[id]/page.tsx` (`showWorkloadRuns` state; "Workload Runs" button; render; row → `setOpenRunId`)
- Test: `src/frontend/src/components/canvas/__tests__/WorkloadRunsModal.test.tsx`

**Interfaces:**
- Consumes: `GET /api/v1/projects/{id}/workloads` → `[{ id, kind, catalog_item, role_fqcn, status, error, created_at }]`.
- Produces: `export default function WorkloadRunsModal({ projectId, onClose, onOpenRun }: { projectId: string; onClose: () => void; onOpenRun: (runId: string) => void })` — plain HTML `<table>`; each row's "View" opens the run via `onOpenRun`.

- [ ] **Step 1: Write the failing test**

Create `src/frontend/src/components/canvas/__tests__/WorkloadRunsModal.test.tsx`:

```tsx
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import WorkloadRunsModal from "@/components/canvas/WorkloadRunsModal";

describe("WorkloadRunsModal", () => {
  beforeEach(() =>
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve([
              {
                id: "run-1",
                kind: "ad_hoc",
                role_fqcn: "some.role",
                catalog_item: null,
                status: "succeeded",
                error: null,
                created_at: "2026-09-13T10:00:00+00:00",
              },
            ]),
        } as Response),
      ),
    ),
  );
  afterEach(() => vi.unstubAllGlobals());

  it("lists runs and opens one on View", async () => {
    const onOpenRun = vi.fn();
    render(<WorkloadRunsModal projectId="p1" onClose={() => {}} onOpenRun={onOpenRun} />);
    await waitFor(() => expect(screen.getByText("some.role")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /view/i }));
    expect(onOpenRun).toHaveBeenCalledWith("run-1");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/prutledg/troshka/src/frontend && npm test -- src/components/canvas/__tests__/WorkloadRunsModal.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

Create `src/frontend/src/components/canvas/WorkloadRunsModal.tsx`:

```tsx
"use client";

import React, { useEffect, useState } from "react";

interface RunItem {
  id: string;
  kind: string;
  catalog_item: string | null;
  role_fqcn: string | null;
  status: string;
  error: string | null;
  created_at: string;
}
interface Props {
  projectId: string;
  onClose: () => void;
  onOpenRun: (runId: string) => void;
}

export default function WorkloadRunsModal({ projectId, onClose, onOpenRun }: Props) {
  const [runs, setRuns] = useState<RunItem[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`/api/v1/projects/${projectId}/workloads`);
        const data = await r.json();
        if (!cancelled) setRuns(Array.isArray(data) ? data : []);
      } catch {
        /* leave empty */
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 10000,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "rgba(0,0,0,0.6)",
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        style={{
          background: "var(--pf-t--global--background--color--primary--default)",
          borderRadius: 12,
          padding: 24,
          width: 720,
          maxWidth: "92vw",
          maxHeight: "80vh",
          overflowY: "auto",
          boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
          border: "1px solid var(--pf-t--global--border--color--default)",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 16 }}>
          <h2 style={{ margin: 0 }}>Workload Runs</h2>
          <button onClick={onClose}>✕</button>
        </div>
        {loading ? (
          <div style={{ opacity: 0.6 }}>Loading…</div>
        ) : runs.length === 0 ? (
          <div style={{ opacity: 0.6 }}>No workload runs yet.</div>
        ) : (
          <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ borderBottom: "1px solid var(--pf-t--global--border--color--default)" }}>
                <th style={{ textAlign: "left", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>TARGET</th>
                <th style={{ textAlign: "left", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>KIND</th>
                <th style={{ textAlign: "left", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>STATUS</th>
                <th style={{ textAlign: "left", padding: "4px 8px", fontSize: 11, opacity: 0.6 }}>CREATED</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.id} style={{ borderBottom: "1px solid var(--pf-t--global--border--color--default)" }}>
                  <td style={{ padding: "6px 8px" }}>{run.role_fqcn || run.catalog_item || "—"}</td>
                  <td style={{ padding: "6px 8px" }}>{run.kind}</td>
                  <td style={{ padding: "6px 8px" }}>{run.status}</td>
                  <td style={{ padding: "6px 8px" }}>{run.created_at?.slice(0, 19).replace("T", " ")}</td>
                  <td style={{ padding: "6px 8px", textAlign: "right" }}>
                    <button className="props-library-btn" onClick={() => onOpenRun(run.id)}>
                      View
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/prutledg/troshka/src/frontend && npm test -- src/components/canvas/__tests__/WorkloadRunsModal.test.tsx`
Expected: PASS

- [ ] **Step 5: Wire the launcher + render**

In `page.tsx`: add `const [showWorkloadRuns, setShowWorkloadRuns] = useState(false);`, an import `import WorkloadRunsModal from "@/components/canvas/WorkloadRunsModal";`, a button in `.project-action-bar-right` (gated on active) next to "Run Workload":

```tsx
          {projectState === "active" && (
            <button
              className="project-publish-btn"
              onClick={() => setShowWorkloadRuns(true)}
              style={{ opacity: 0.85 }}
            >
              Workload Runs
            </button>
          )}
```

And the render near the other modals:

```tsx
      {showWorkloadRuns && (
        <WorkloadRunsModal
          projectId={projectId}
          onClose={() => setShowWorkloadRuns(false)}
          onOpenRun={(runId) => {
            setShowWorkloadRuns(false);
            setOpenRunId(runId);
          }}
        />
      )}
```

- [ ] **Step 6: Full frontend suite + commit**

```bash
cd /Users/prutledg/troshka/src/frontend && npm test
cd /Users/prutledg/troshka && git add src/frontend/src/components/canvas/WorkloadRunsModal.tsx src/frontend/src/components/canvas/__tests__/WorkloadRunsModal.test.tsx src/frontend/src/app/projects/\[id\]/page.tsx && git commit -m "feat(workloads): run-history modal + launcher"
```

Expected: full frontend suite green.

---

## Self-Review Notes (author)

- **Spec coverage:** §1a→Task 2; §1b→Task 1; §1c→Task 3; §1d→Task 4; §2→Task 5; §3→Task 7; §4→Task 8 (+ Task 6 WS nudge); §5→Task 9. Deferred items (catalog UI, live VM-path validation, global route) intentionally excluded.
- **Deviation from spec §3:** the "Run Workload" button gates only on `projectState === "active"` (not on `ocp_control_plane_usable_at`, which isn't readily available client-side); the backend still enforces the OCP-readiness 409, and the form surfaces `detail` inline. Honest and simpler.
- **Simplification:** reuse `WorkloadRun.log_ref` for the persisted tail → no migration (spec said "new column"; this is strictly less work and the column already exists unused).
- **Type consistency:** `get_workload_log(db, run)`, `_should_validate_inventory(target_map)`, `preview_inventory(topology)`, and the `{ id, status, log }` / `{ groups, errors }` response shapes are used identically across backend tasks and frontend fetch sites.

## Post-Implementation (do NOT auto-run)

- Backend has no auto-reload — tell the user to `./dev-services.sh restart backend` **and** `./dev-services.sh restart worker` (the finalize/log changes run in the worker) after Tasks 1–4.
- KubeVirt live path needs `deploy-full.sh`; troshkad path needs only the local restarts (per project memory). Do not push/promote without asking.
