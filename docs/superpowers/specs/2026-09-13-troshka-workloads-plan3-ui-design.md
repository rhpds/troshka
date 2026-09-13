# Troshka Workloads — Plan 3: UI (Ad-Hoc) — Design

> Third and final phase of the Troshka workloads subsystem. Plan 1 =
> foundational backend (secret store, vault, repo cache, merge engine,
> resolver). Plan 2 = execution (runner pod, inventory, cluster access,
> `WorkloadRun`, RQ jobs) + the cluster-access rework. **Plan 3 = the user-facing
> UI** on top of the already-shipped run API, plus the small backend additions the
> UI needs to observe a run.

**Spec (parent design):** `docs/superpowers/specs/2026-09-11-troshka-workloads-design.md`
**Depends on:** Plan 1 + Plan 2 + cluster-access rework — all merged to `main`.

## Goal

Let a user trigger, observe, and review an **ad-hoc** workload run entirely from
the Troshka UI — no more `curl`. A run targets **either an OCP cluster or project
VMs** (via the `troshka.cloud` inventory). Everything is project-scoped and lives
on the existing project canvas (action-bar button + modals); no new routing and no
tabbed project shell (none exists).

## Scope

**In scope:**
1. Backend: a run-log read endpoint, log persistence, an inventory-preview/validate
   endpoint, and `target_map` mode consumption.
2. Canvas: a first-class "Ansible groups" field on VM nodes.
3. UI: an ad-hoc **run trigger form** (modal), a **run-detail** modal (live
   progress + logs), and a **run-history** list (modal/drawer).

**Out of scope (deferred to a follow-up):**
- Catalog-item selection UI, the `list-catalog` backend endpoint, and seeding an
  example agnosticv catalog item. (`kind=catalog_item` remains API/`curl`-only and
  is still unproven live.)
- Live end-to-end validation of the VM-inventory target path. The backend inventory
  emitter + `AnsibleGroup` validation are code-complete (Plan 2) but every live E2E
  run so far targeted an OCP cluster; the first live VM-targeted run is a follow-up.
- A cross-project global `/workloads` route/nav entry. Plan 3 is per-project only.

## Decisions

| # | Decision |
|---|----------|
| P3-1 | **Ad-hoc only** in the UI. `kind=ad_hoc` (`role_fqcn` + `requirements_content`) is the only path proven live E2E; catalog selection is deferred. |
| P3-2 | **Placement = canvas action bar + modals.** A "Run Workload" button on `projects/[id]/page.tsx` opens the run form; run-detail and run-history are modals. No new route; reuses the established modal patterns (`ClusterInstallLogModal`, `SavePatternModal`, …). |
| P3-3 | **Progress/logs transport = Approach 3 (poll + WS nudge).** A run-log endpoint polled on an interval is the source of truth (survives refresh/reconnect/history); the existing `workload-progress` WebSocket message triggers an immediate refetch for a near-real-time feel. WS is an optimization, never a dependency. |
| P3-4 | **VM targeting = first-class canvas field + run-form preview.** A dedicated "Ansible groups" input on VM nodes writes `data.tags.AnsibleGroup`; the run form previews + validates the derived inventory (bastion/name/first-NIC-IP contract) before launch. |
| P3-5 | **`target_map` gains a `mode`.** `{"mode": "cluster"|"vms", "clusters"?: [...], "vm_groups"?: [...]}`. Consumed by `run_service` to choose validation/delivery. Null `target_map` = today's behavior (backward compatible). |
| P3-6 | **Final logs are persisted** (bounded tail) so run history shows completed-run logs after the runner pod is GC'd. |

## Current State (what already exists — do not rebuild)

- **Run API (shipped):** `POST /projects/{id}/workloads` (trigger, 202),
  `GET /projects/{id}/workloads` (list runs), `GET /workloads/{run_id}` (status +
  Redis `progress`). All owner/admin guarded via `_enforce_project_access`.
- **`WorkloadRun` model** with `target_map` (JSONB) and `requirements_content`
  (JSONB) columns; `target_map` is **stored but not yet consumed** by the pipeline.
- **Both target channels are already delivered every run** (`run_service.py:98–125`):
  the VM inventory is always emitted (`build_inventory_yaml` +
  `validate_ansible_groups(topo, require_bastion=(not _has_ocp(project)))`), and when
  the project has OCP the kubeconfig + in-pod cluster-admin mint prelude are delivered
  (`pod_launch.py`). The **role's `hosts:`/config decides** which channel it uses.
- **Monitor already emits a WS message** `{"type": "workload-progress",
  "progress": {"step": "<last Ansible TASK>"}}` via `notify_project`
  (`run_service.py:396`) — the project socket the canvas already consumes.
- **Progress is task-name only.** `parse_workload_progress` returns `{"step": ...}`;
  full logs are **read live from the pod** each monitor tick
  (`_read_runner_pod_logs`) but **never persisted** — `_finalize_workload_run` stores
  only `status` and (on failure) `error[:2000]`. There is **no** `GET .../log`
  endpoint today.
- **Inventory contract** (`inventory.py`): every VM needs `data.name`, a first-NIC
  IP, and a non-empty `data.tags.AnsibleGroup`; in bastion mode exactly one VM tagged
  `bastions` with an external IP. Cluster-member VMs are auto-tagged by
  `clusterMaterialize.ts`; a **standalone untagged VM** currently blocks any run.
- **Frontend facts:** relative `fetch("/api/v1/...")` (proxied by
  `src/app/api/v1/[[...path]]/route.ts` → `BACKEND_URL`); no shared API client; dev
  auth via `localStorage` token; live updates via `useVmStateSocket` (project WS,
  handles typed messages incl. `deploy-progress`); `data.tags` is
  `Record<string,string>`, canvas-editable in `PropertiesPanel.tsx` (~2274–2336) and
  auto-saved; `projects/[id]/page.tsx` is a full-screen canvas with an action bar
  (~718) — **no tabbed detail page**; `ClusterInstallLogModal.tsx` is the reference
  poll-a-log-endpoint + auto-scrolling `<pre>` + derived-stage status pattern.

## Design

### §1 — Backend additions (the only non-frontend work)

**§1a — `GET /workloads/{run_id}/log`**
- Response: `{ id, status, log }`.
- While the run is non-terminal: read live pod logs via `_read_runner_pod_logs(host,
  run_id)`, resolving `host` from `run.project_id → Project.host_id → Host`.
- After completion (or when the pod/host is unavailable, e.g. orphan run): serve the
  persisted `log_text`.
- Auth identical to `GET /workloads/{run_id}`: owner/admin via
  `_enforce_project_access`; orphan run (no `project_id`) → admin-only.
- Failure to read live logs falls back to the persisted log rather than 500-ing.

**§1b — Persist final log**
- New column `WorkloadRun.log_text` (SQLAlchemy `Text`, nullable). Register nothing
  new in `__init__.py` (model already registered); add an Alembic migration chained
  to the **current head** (verify with `alembic heads` at implementation time).
- `_finalize_workload_run(run_id, status, error_or_logs)` already receives the logs;
  persist a **bounded tail** (last ~256 KB) to `log_text` for all terminal statuses
  (not only errors). Keep the existing `error[:2000]` behavior for the error field.

**§1c — `POST /projects/{id}/workloads/inventory-preview`**
- Body: `{ target_map }` (or empty → whole-project preview).
- Returns `{ groups: {group: [host,...]}, errors: [str,...] }` by reusing
  `build_inventory_yaml` (parsed to structure, or a small pure helper that returns the
  group→host map) + `validate_ansible_groups` (catching `InventoryError` into
  `errors`). Uses `project.deployed_topology or project.topology`.
- Owner/admin guarded. Read-only; makes no cluster/pod calls.

**§1d — `target_map` mode consumption**
- Shape: `{"mode": "cluster"|"vms", "clusters"?: [str], "vm_groups"?: [str]}`.
- In `run_service` (the existing branch at 98–125):
  - `mode == "cluster"`: deliver cluster access as today; **skip** the whole-project
    `AnsibleGroup` validation (fixes the standalone-untagged-VM edge). Inventory may
    still be emitted for cluster-member hosts but is not gated on user VM tags.
  - `mode == "vms"`: `validate_ansible_groups` (require_bastion per SSH mode) and emit
    inventory; if `vm_groups` is provided, validate those groups exist.
  - `target_map` null/absent: **unchanged** current behavior (backward compatible).
- No change to `pod_launch.py`'s artifact delivery mechanics.

### §2 — Canvas: first-class "Ansible groups" field

- Add a labeled "Ansible groups" input to the `vmNode` block of
  `PropertiesPanel.tsx` (near the existing Tags section, ~2274), comma-separated,
  reading/writing `data.tags.AnsibleGroup` through the existing `update("tags", …)` /
  `updateNodeData` path (so it persists + auto-saves like any tag).
- It is a typed view over an existing tag — no store/schema change, no new
  topology field. Auto-set cluster-member `AnsibleGroup` tags remain untouched; the
  field simply surfaces/edits whatever is there.
- Light inline hint of the contract (name + first-NIC IP required; one `bastions`
  with external IP in SSH mode) so users tag correctly; authoritative validation is
  the §1c preview at run time.

### §3 — Run trigger form (modal)

- **Entry point:** a "Run Workload" button in the canvas action bar
  (`projects/[id]/page.tsx` ~718, alongside Save as Pattern / Export Template).
- **Client-side gating** (mirror the API 409s so the button is honest): enabled only
  when `project.state === "active"`, and for OCP projects only when
  `ocp_control_plane_usable_at` is set. Otherwise disabled with a tooltip reason.
- **Fields:**
  - `role_fqcn` (text, required).
  - `requirements_content` — a small repeatable rows editor for git-sourced
    collections: `name` (git URL), `type` (default `git`), `version` (default `main`).
    Serialized to `{"collections": [...]}`.
  - EE image (optional text; placeholder shows the config default).
  - **Target selector:** Cluster | VMs (radio/toggle).
    - Cluster mode → `target_map = {"mode": "cluster"}` (optionally a cluster picker
      if >1; default `default`).
    - VMs mode → calls §1c `inventory-preview`, renders the derived groups/hosts and
      any contract errors; **Launch disabled while errors exist**; sends
      `target_map = {"mode": "vms", "vm_groups": [...selected]}`.
- **Submit:** `POST /projects/{id}/workloads` → on **202** close the form and open the
  §4 run-detail modal for the returned `id`. Surface 403/404/409 via the existing
  `AlertModal`/inline-error conventions.

### §4 — Run-detail modal (live progress + logs)

- Modeled on `ClusterInstallLogModal`: an auto-scrolling `<pre>` fed by polling
  `GET /workloads/{run_id}/log` every ~3 s, with `GET /workloads/{run_id}` for
  `status` + `progress.step`. Header: status badge + current TASK.
- **WS nudge (Approach 3):** subscribe to the existing project socket; on a
  `workload-progress` message for this run, refetch immediately (no reliance on WS
  for correctness — polling still runs).
- Terminal `status` (`succeeded`/`error`/`timeout`) stops polling, shows the final
  status and, on failure, the `error`. A "Copy log" affordance (reuse the existing
  copy pattern).

### §5 — Run-history (modal/drawer)

- A "Workload runs" launcher in the action bar opens a list of
  `GET /projects/{id}/workloads` (PatternFly `Table`/list): status, kind, role/catalog,
  created-at; newest first (API already orders `created_at.desc()`).
- Each row opens the §4 run-detail modal for that `run_id`. Empty state when none.

## Data Flow

```
Canvas action bar ─"Run Workload"─▶ Run form (§3)
   │  (VMs mode) POST /projects/{id}/workloads/inventory-preview ──▶ preview+validate
   └─ POST /projects/{id}/workloads {kind:ad_hoc, role_fqcn, requirements_content,
                                     ee_image?, target_map:{mode,...}}  ─202▶ run_id
                                                       │
Run-detail modal (§4) ◀───────────────────────────────┘
   ├─ poll GET /workloads/{run_id}/log        (log text; live→persisted)
   ├─ poll GET /workloads/{run_id}            (status + progress.step)
   └─ WS "workload-progress" ─▶ immediate refetch
Run-history modal (§5) ─ GET /projects/{id}/workloads ─▶ rows ─▶ Run-detail modal
```

## Error Handling

- **Form:** disabled-with-reason for inactive/not-workload-ready projects; VM-mode
  Launch disabled while `inventory-preview` reports contract errors; POST 4xx →
  `AlertModal`/inline error.
- **Log endpoint:** live-read failure falls back to persisted `log_text`; never 500 on
  a transient pod read.
- **Run-detail:** on terminal status, stop polling; render `error` on failure.
- **Auth:** every new endpoint reuses `_enforce_project_access` (owner/admin; orphan →
  admin), matching the shipped endpoints — no new scope, no IDOR surface.

## Testing

**Backend (pytest, SQLite, all I/O mocked; add trailing `time.time()` values):**
- `GET .../log`: running (live read mocked) vs completed (persisted) vs orphan
  (admin-only) vs non-owner 403 vs 404; live-read failure → persisted fallback.
- Log persistence: `_finalize_workload_run` writes `log_text` for all terminal
  statuses; tail bound enforced.
- `inventory-preview`: valid topology → groups map; missing name/IP/tag and
  bastion-count violations → `errors`; auth guard.
- `target_map` mode: `cluster` skips VM validation (untagged standalone VM does **not**
  block); `vms` validates + scopes to `vm_groups`; null = unchanged behavior.

**Frontend (existing component-test conventions):**
- Canvas field writes `data.tags.AnsibleGroup` via `update("tags", …)`.
- Form: gating on project state/OCP readiness; VM-mode Launch disabled on preview
  errors; correct `target_map` payload per mode.
- Run-detail: polls log+status; `workload-progress` WS triggers refetch; stops on
  terminal status.

## Constraints (carried from Plans 1–2)

- Python 3.13; SQLAlchemy 2.0 + Alembic (migration chained to current head, auto-runs
  on startup); RQ/Redis; FastAPI; Dynaconf. Cognitive complexity ≤ 15/function
  (extract helpers). System `black`; zero `pyright` errors in touched files. Never
  `drop_all` the shared test engine. Secrets never in argv/logs. Do **not** modify the
  OCP install path or the shipped run/monitor pipeline beyond the additive changes in
  §1. Git: no `Co-Authored-By`; never amend; commit from repo root with absolute paths;
  work on a branch `feat/workloads-plan3-ui` (not `main`).
- Frontend is a patched Next.js (`src/frontend/AGENTS.md`) — check
  `node_modules/next/dist/docs/` before writing Next.js-specific code; follow existing
  PatternFly 6 + raw-`fetch` + `useState`/`useEffect` conventions.

## Deferred / Follow-ups

- Catalog-item UI + `GET /projects/{id}/workloads/catalog` (list) + example agnosticv
  item + first live `kind=catalog_item` run.
- First live VM-inventory-targeted run (validate the emitter/contract end-to-end).
- Cross-project global `/workloads` view.
- Richer `target_map` (per-cluster naming beyond `default`; multi-cluster).
