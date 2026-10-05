# Project Metering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Native Troshka metering: ledger of usage intervals, live spend, budget warn/auto-stop, invoices after destroy, Metering nav — never talking to reporting-api.

**Architecture:** Pure reconcile math over a topology snapshot; persist `metering_intervals`; poll + event hooks; freeze `project_invoices` before project delete. Rates: DB type defaults + `hosts.metering_rates` override. Budget uses existing auto-stop spawn.

**Tech Stack:** FastAPI, SQLAlchemy 2, Alembic, RQ/timer thread, Next.js 15, PatternFly 6

**Spec:** `docs/superpowers/specs/2026-10-05-project-metering-design.md`

## Global Constraints

- No HTTP to reporting-api / demo-reporting from metering code
- CPU/RAM only while VM running; disk, EIP, Ceph while the resource exists
- $0 rate if unset; host override beats type default
- Budget 80% warn once, 100% auto-stop once (`budget_stopped`); persistent resources may still accrue
- Invoice has no ON DELETE CASCADE from `projects`
- `metering.enabled` false → no poll, APIs 404
- Cognitive complexity ≤ 15 per function (extract helpers)
- Tests: `cd src/backend && ./venv/bin/python3 -m pytest …`; extra trailing `time.time()` mock values
- Git add with absolute paths from repo root

## File map

| Path | Role |
| --- | --- |
| `src/backend/app/models/metering.py` | `MeteringRateDefault`, `MeteringInterval`, `ProjectInvoice` |
| `src/backend/app/models/project.py` | `budget_usd`, `budget_warned`, `budget_stopped` |
| `src/backend/app/models/host.py` | `metering_rates` JSONB |
| `src/backend/alembic/versions/b7e8f9a0c1d2_add_project_metering.py` | migration (`down_revision`: `a1c2e3f4b5d6`) |
| `src/backend/app/services/metering_math.py` | rates, snapshot, interval diff, spend (no DB) |
| `src/backend/app/services/metering_service.py` | persist, reconcile, budget, invoice, poll |
| `src/backend/app/api/metering.py` | rates, invoices, project spend |
| `src/backend/app/api/projects.py` | `budget_usd` on update/response; destroy freeze; hooks |
| `src/backend/app/api/hosts.py` | `metering_rates` on GET/PATCH |
| `src/backend/app/main.py` | router + start metering poll with project_timer |
| `src/backend/tests/test_metering_math.py` | unit tests |
| `src/backend/tests/test_metering_api.py` | API tests |
| `src/frontend/src/app/layout.tsx` | Metering nav |
| `src/frontend/src/app/metering/page.tsx` | invoices + active |
| `src/frontend/src/app/metering/[id]/page.tsx` | invoice detail |
| `src/frontend/src/app/admin/metering/page.tsx` | type rates |
| `src/frontend/src/app/projects/[id]/page.tsx` | live spend + budget field |
| `deploy/helm/templates/backend-config.yaml` | `metering.enabled` / `poll_seconds` / `warn_ratio` |

---

### Task 1: Schema and models

**Files:**
- Create: `src/backend/app/models/metering.py`
- Create: `src/backend/alembic/versions/b7e8f9a0c1d2_add_project_metering.py`
- Modify: `src/backend/app/models/__init__.py`
- Modify: `src/backend/app/models/project.py` (budget columns after `auto_stopped`)
- Modify: `src/backend/app/models/host.py` (`metering_rates` JSONB nullable)
- Modify: `src/backend/app/schemas/host.py` (`metering_rates: dict | None = None` on response)
- Modify: `src/backend/app/schemas/project.py` (`budget_usd`, `budget_warned`, `budget_stopped` on response; `budget_usd` on `ProjectUpdate`)

**Interfaces:**
- Produces: models `MeteringRateDefault(provider_type PK, rates JSONB)`, `MeteringInterval`, `ProjectInvoice`; project budget fields; host `metering_rates`

- [ ] **Step 1: Add models**

`MeteringInterval.kind` values: `vcpu`, `ram`, `disk`, `eip`, `ceph`. `ended_at` nullable. `qty` and `unit_rate` `Numeric(18, 6)`. Partial unique index: one open row per `(project_id, kind, resource_id)` where `ended_at IS NULL` (PostgreSQL; SQLite tests can skip the partial unique and rely on service logic).

`ProjectInvoice.project_id` `String(36)` **without** FK to `projects`. `owner_id` FK to `users.id` ON DELETE SET NULL.

- [ ] **Step 2: Alembic** `b7e8f9a0c1d2`, revises `a1c2e3f4b5d6`. UUID columns `postgresql.UUID(as_uuid=False)`. SQLite-safe: no `server_default` that breaks tests beyond Boolean false / numeric 0.

- [ ] **Step 3: Register models in `__init__.py`**

- [ ] **Step 4: Commit** `feat(metering): add ledger, invoice, budget, and host rate columns`

---

### Task 2: Pure math (TDD)

**Files:**
- Create: `src/backend/app/services/metering_math.py`
- Test: `src/backend/tests/test_metering_math.py`

**Interfaces:**
- Produces:
  - `RATE_KEYS = ("vcpu_hour", "ram_gib_hour", "disk_gib_hour", "eip_hour", "ceph_gib_hour")`
  - `KIND_TO_RATE = {"vcpu": "vcpu_hour", "ram": "ram_gib_hour", "disk": "disk_gib_hour", "eip": "eip_hour", "ceph": "ceph_gib_hour"}`
  - `resolve_rate(kind: str, provider_type: str, host_rates: dict | None, type_rates: dict[str, dict]) -> float`
  - `hours_between(start, end) -> float`
  - `interval_cost(qty, unit_rate, start, end) -> float`
  - `spend_total(rows: list[dict], now) -> float` — each row `{qty, unit_rate, started_at, ended_at}`
  - `desired_resources(snapshot: dict) -> set[tuple[str, str, float, str | None, str]]`  
    snapshot: `{vms: [{id, running, vcpus, ram_gib, host_id}], disks: [{id, size_gib, host_id}], eips: [{id, host_id}], ceph: [{id, size_gib, host_id}], provider_type, host_id}`  
    yields `(kind, resource_id, qty, host_id, provider_type)`  
    running VMs → vcpu + ram; always disks/eips/ceph if present
  - `diff_intervals(open_rows: list[tuple], desired: set) -> tuple[list, list]`  
    open_rows `(kind, resource_id, qty)`; returns `to_close`, `to_open` (open includes qty)

- [ ] **Step 1: Failing tests** in `test_metering_math.py`:
  - host override 0.05 beats type 0.02 for `vcpu`
  - missing rate → 0.0
  - 2 vCPU × $0.10 × 1.5 h = 0.30
  - closed + open through `now`
  - desired: stopped VM → no vcpu/ram, still disk
  - diff: missed stop → close vcpu; new disk → open disk

- [ ] **Step 2: Implement `metering_math.py`** (keep each function complexity ≤ 15)

- [ ] **Step 3:** `./venv/bin/python3 -m pytest tests/test_metering_math.py -v` from `src/backend`

- [ ] **Step 4: Commit** `feat(metering): add rate lookup and interval diff math`

---

### Task 3: Persistence, reconcile, budget, invoice

**Files:**
- Create: `src/backend/app/services/metering_service.py`
- Test: `src/backend/tests/test_metering_service.py`
- Modify: `src/backend/app/services/project_timer.py` — after timer loop, call `reconcile_all_projects` when enabled
- Modify: `src/backend/app/core/config.py` usage via `getattr(config, "metering", None)` — do **not** put secrets in `config.yaml`; Helm + `config.local.yaml` optional. Defaults in code: `enabled=True`, `poll_seconds=120`, `warn_ratio=0.8`

**Interfaces:**
- Consumes: `metering_math`, models, `Project`, `Host`, `Provider`, `ElasticIp`, `Disk`
- Produces:
  - `metering_enabled() -> bool`
  - `build_snapshot(db, project) -> dict | None` (None if draft / no host)
  - `reconcile_project(db, project, now=None) -> float` spend after persist
  - `apply_budget(db, project, spend) -> None` 80% warn + WS; 100% spawn existing stop, set `budget_stopped`
  - `freeze_invoice(db, project, now=None) -> ProjectInvoice`
  - `live_spend(db, project, now=None) -> dict`

Stop spawn: same as `project_timer._spawn_stop(project.id)` (import that function or a shared helper — do not duplicate stop logic).

Ceph: topology nodes with type `storageNode` / ceph flags and a size; if none, empty list. Provisioned GiB.

EIP: `ElasticIp` rows for `project_id` with `state` associated/allocated as used today.

VM running: topology `vmNode` `data.state`/`running`/`power` matching how the UI marks running; also `VM` table power if that is source of truth — prefer **deployed_topology** then topology. Treat `running`/`active`/libvirt running as running.

- [ ] **Step 1: Tests** (SQLite session from conftest):
  - start running VM → open vcpu+ram; stop → close those, disk stays open
  - poll closes vcpu if snapshot says not running
  - rate change: new interval new rate; closed unchanged
  - budget 80% sets `budget_warned`; 100% calls stop once (`budget_stopped`)
  - freeze_invoice then delete project; invoice row remains

- [ ] **Step 2: Implement service**

- [ ] **Step 3: pytest `tests/test_metering_service.py`**

- [ ] **Step 4: Commit** `feat(metering): persist intervals, budget stop, and invoices`

---

### Task 4: HTTP API + project/host fields

**Files:**
- Create: `src/backend/app/api/metering.py` (`APIRouter(prefix="/metering", tags=["metering"])`)
- Modify: `src/backend/app/main.py` `include_router`
- Modify: `src/backend/app/api/projects.py` PATCH `budget_usd`; GET include budget fields; **before** delete, `freeze_invoice` + reconcile
- Modify: `src/backend/app/api/hosts.py` PATCH/response `metering_rates`
- Hook: after VM start/stop and deploy active — `reconcile_project` (keep hooks thin; one helper `touch_metering(db, project)`)
- Test: `src/backend/tests/test_metering_api.py`

**Interfaces:**
- `GET /api/v1/metering/rates` — all users; `{ "aws": { "vcpu_hour": 0.0, ... }, ... }` merged DB over empty keys
- `PUT /api/v1/metering/rates` — admin; body same map
- `GET /api/v1/projects/{id}/metering` — `{ total_usd, by_kind, budget_usd, budget_warned, budget_stopped, currency: "USD" }`
- `GET /api/v1/metering/invoices` — owner; admin `?all=true`
- `GET /api/v1/metering/invoices/{id}`
- If not `metering_enabled()`: 404

- [ ] **Step 1: API tests** (existing FastAPI client fixture): rates PUT admin; project metering; freeze via delete; invoice GET after delete; enabled false 404 (monkeypatch)

- [ ] **Step 2: Implement router + hooks + destroy order**

- [ ] **Step 3: pytest `tests/test_metering_api.py`**

- [ ] **Step 4: Commit** `feat(metering): expose rates, live spend, and invoices API`

---

### Task 5: Frontend — nav, invoices, live spend, admin rates

**Files:**
- Modify: `src/frontend/src/app/layout.tsx` — nav `{ label: "Metering", path: "/metering" }` after Projects; `titleMap`
- Create: `src/frontend/src/app/metering/page.tsx` — tabs Invoices / Active; PatternFly table
- Create: `src/frontend/src/app/metering/[id]/page.tsx`
- Create: `src/frontend/src/app/admin/metering/page.tsx` + admin nav item “Metering rates”
- Modify: project canvas page — banner spend/budget; settings `budget_usd`
- Modify: `src/frontend/src/app/admin/hosts/page.tsx` — optional JSON/fields for `metering_rates` (keep small: five number inputs)

Icon: `@patternfly/react-icons/dist/esm/icons/dollar-sign-icon`.

- [ ] **Step 1: Nav + `/metering` list + detail**

- [ ] **Step 2: Project banner + budget field**

- [ ] **Step 3: Admin rates + host overrides**

- [ ] **Step 4: Helm `metering.enabled: true`, `poll_seconds: 120`, `warn_ratio: 0.8` in `backend-config.yaml` under the config.yaml literal

- [ ] **Step 5: Commit** `feat(metering): add Metering nav, invoices, and live spend UI`

---

### Task 6: Wire poll + docs pointer

**Files:**
- Modify: `src/backend/app/main.py` lifespan next to `start_project_timer` → `start_metering_poll()` in `metering_service`
- Modify: `docs/superpowers/specs/2026-10-05-project-metering-design.md` status Implemented
- Modify: `docs/superpowers/plans/2026-10-03-reporting-api-ingest.md` one line: metering is separate, no payloads

- [ ] **Step 1: Poll thread/RQ** every `poll_seconds`; skip if disabled

- [ ] **Step 2: pytest still green** `tests/test_metering_math.py tests/test_metering_service.py tests/test_metering_api.py`

- [ ] **Step 3: Commit** `feat(metering): start reconcile poll with project timers`

---

## Spec coverage

| Spec | Task |
| --- | --- |
| Rate card type + host | 1, 2, 4, 5 |
| Ledger kinds + accrual rules | 2, 3 |
| Hybrid poll + events | 3, 4, 6 |
| Budget 80/100 auto-stop | 3, 5 |
| Invoice on destroy | 3, 4, 5 |
| Metering nav | 5 |
| No reporting-api | 3–4 tests + ingest plan note |
| Helm config | 5 |
| Ceph provisioned GiB | 3 snapshot |
