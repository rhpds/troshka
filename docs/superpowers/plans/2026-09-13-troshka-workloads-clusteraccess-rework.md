# Troshka Workloads — Cluster-Access Rework + Recert-Usable Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make workload runs actually work end-to-end by moving all cluster-touching work off the backend and into the project-namespace runner pod, and gate runs on a "minimal control-plane-usable" recert milestone that's surfaced with its own timestamp.

**Architecture:** Backend-minimal (spec D12): the backend resolves the catalog item (agnosticv merge — stays central for private creds), delivers artifacts + the stored admin **kubeconfig**, launches the runner pod, and tracks state. It makes **no cluster API calls**. The runner pod (which can resolve the in-cluster `api.ocp.local`) mints the cluster-admin SA token via agnosticd's `openshift_cluster_admin_service_account` role and runs the workloads. Runs are gated on a recert milestone computed **in the ops-pod** (in-cluster) and persisted to the `Project` row, so the gate is a pure backend state check.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2 + Alembic, RQ; agnosticd-v2 ansible (in-pod); Next.js/PatternFly frontend (Status & Log panel). Tests: pytest (SQLite), all external I/O mocked.

**Spec:** `docs/superpowers/specs/2026-09-11-troshka-workloads-design.md` (see **D7** in-pod cluster access, **D12** backend-minimal, **D13** recert-usable gate + timestamp).

**Branch base:** `feat/workloads-runner-fetch` (contains: runner clones agnosticd-v2 at runtime `b2c27abf`, `ANSIBLE_CONFIG` fix `3a098e48`, pullable default EE `1e3bfcf9`, spec rework `c94ca0c8`). Start the rework from this branch (or main after it merges).

## Background (why — validated live 2026-09-13)

A live E2E on a throwaway SNO surfaced that the backend cannot resolve the stored kubeconfig's server `api.ocp.local` (in-cluster-only), so `cluster_access._mint_admin_token` fails with `NameResolutionError`. Only pods in the project network can reach it. Hence cluster access must be resolved in-pod. Separately, the user requires recert to be complete before any workload/agnosticd op, defined as "minimal control-plane-usable" (not "console up", which is a false-ready signal while the apiserver cert-revision rollout is still in flight).

## Global Constraints

- **Python 3.13.** Add trailing values to any `time.time()` mocks (avoid `StopIteration`).
- **Tests** from `src/backend`: `./venv/bin/python3 -m pytest <path> -v`. Frontend from `src/frontend` per its tooling.
- **Pyright**: the backend pre-commit hook runs `pyright app/` (whole package) — run that (not just per-file) and get **0 errors, 0 warnings**. Use `pyright` (npm global). Guard every Optional (`db.get(...)`) with `assert ... is not None` or an explicit branch.
- **Pristine**: no unused imports; `_`-prefix intentionally-unused loop/lambda params. Frontend: `tsc` clean.
- **Cognitive complexity ≤ 15/function** (SonarQube S3776).
- **D12 backend-minimal**: the backend must make **no cluster API calls** anywhere in the workloads path. Any operator/cluster-state check happens **in the ops-pod** and is persisted to `Project`; the backend only reads persisted state.
- **D7 credential isolation**: git creds + vault key never leave the backend; the runner pod receives resolved artifacts + the admin kubeconfig (0600 mount) only. The admin kubeconfig is delivered as a read-only mount, never in argv/env.
- **Do NOT modify the OCP install path** (`deploy_service._deploy_ops_pod*`, the OCP install builders) except the recert-milestone additions in Task 1, which must not change existing install behavior.
- **Git**: no `Co-Authored-By`; never amend; commit from repo root (`cd /Users/prutledg/troshka && git add src/backend/...`). Work on `feat/workloads-runner-fetch` (or a fresh branch off it). **Do not push without the user's OK.**

## File Structure

Modify:
- `src/backend/app/models/project.py` (+ migration) — persisted recert-usable milestone fields.
- `src/backend/app/services/deploy_service.py` — recert monitor computes/persists the milestone (recert path only; do not disturb install path). ⚠️ MTU session also edits this file — coordinate/rebase.
- `src/backend/app/services/ocp/ops_pod_install.py` (or the recert script builder) — emit a "control-plane-usable" marker + parse it.
- `src/backend/app/services/workloads/run_service.py` — kubeconfig delivery; in-pod token mint wiring; recert-usable gate; drop `resolve_cluster_access` usage.
- `src/backend/app/services/workloads/pod_launch.py` — `build_run_command` runs the cluster-admin SA role + builds the `clusters` dict; artifact map includes the kubeconfig.
- `src/backend/app/api/projects.py` — remove `get_cluster_access` endpoint.
- `src/backend/app/core/auth.py` — remove the `"get_cluster_access": "cluster:access"` allowlist entry.
- `src/backend/app/services/workloads/run_key.py` — drop the `cluster:access` scope (keep `topology:read`, `vm:exec`).
- `src/frontend/…` the recert **Status & Log** panel component — show the milestone timestamp.

Delete:
- `src/backend/app/services/workloads/cluster_access.py` + `tests/test_workload_cluster_access.py` + `tests/test_workload_cluster_access_endpoint.py` (backend SA-mint + endpoint removed).

---

### Task 1: Recert "minimal control-plane-usable" milestone (in-pod compute → persisted)

The ops-pod recert flow runs in-cluster and already "waits on operators". Emit a marker the moment the minimal control-plane-usable condition is met, parse it in the monitor, and persist a timestamp + elapsed on the `Project` row.

**Definition (D13):** `kube-apiserver` Progressing=False + Available=True, `authentication` Available=True, no clusteroperator Degraded=True, with SNO tolerance for operators that are only cosmetically Progressing (e.g. a 2-replica Deployment that can't place a 2nd pod on one node).

**Files:** `ocp/ops_pod_install.py` (recert script + progress parse), `deploy_service.py` (monitor persist), `models/project.py` + migration, tests.

**Interfaces (Produces):**
- `Project.ocp_control_plane_usable_at: datetime | None`, `Project.ocp_control_plane_usable_elapsed: int | None` (seconds).
- A pure parser addition: recognize the marker line and, in the monitor, set the fields once (first time seen), publish a progress item `{"step":"control-plane-usable","detail":"reached at Xm Ys","at":<iso>}`.

- [ ] **Step 1 (grounding):** Read the recert script builder (`build_ops_pod_recert_script` in `ocp/ops_pod_install.py`) and the recert loop's operator-wait. Determine where to emit a marker like `echo "[<clusterId>] control-plane-usable"` when the D13 condition first holds (the script already inspects `oc get co`; add the condition check + one-time marker). Cite the exact wait loop.
- [ ] **Step 2:** Add the `Project` columns + Alembic migration (chain to current head via `alembic heads`; `postgresql`-typed DateTime/Integer nullable). Register nothing new (columns on existing model). RED/GREEN a model test.
- [ ] **Step 3:** Extend the recert progress parser (pure fn) to detect the marker; unit-test it maps the marker → a `control-plane-usable` phase/flag.
- [ ] **Step 4:** In the recert monitor (`_monitor_ops_pod_install` recert branch), when the marker is first parsed, persist `ocp_control_plane_usable_at = now`, `..._elapsed = elapsed`, and publish the progress item. Idempotent (set once). Unit-test with a mocked log stream.
- [ ] **Step 5:** black + `pyright app/` 0/0/0; commit `feat(workloads): persist minimal control-plane-usable recert milestone`.

---

### Task 2: Recert-usable gate on workload runs (backend state check only)

**Files:** `app/services/workloads/run_service.py`, `app/api/workloads.py` (surface 409), tests.

**Interfaces:** `start_workload_run` (and/or the POST handler) raises/returns **409** unless the target project is workload-ready: for an OCP-targeting run, require `project.ocp_control_plane_usable_at is not None` (set in Task 1). For a VM-only run, require `project.state == "active"`. **No cluster call** — read persisted `Project` fields only.

- [ ] TDD: test that a run against a project with `ocp_control_plane_usable_at=None` → 409; with it set → proceeds (enqueues). Then implement. black + `pyright app/` 0/0/0. Commit `feat(workloads): gate runs on minimal control-plane-usable`.

---

### Task 3: Remove backend cluster-access (SA mint + endpoint + scope)

**Files:** delete `cluster_access.py` + its 2 tests; edit `run_service.py` (drop `resolve_cluster_access` call), `api/projects.py` (remove `get_cluster_access`), `core/auth.py` (remove allowlist entry), `run_key.py` (drop `cluster:access` scope), `tests/test_workload_run_key.py`.

- [ ] Remove the code + tests; ensure `run_workload_job` no longer calls `resolve_cluster_access` (Task 4 replaces the mechanism). `pyright app/` 0/0/0 (no dangling imports). Full workload test module set green. Commit `refactor(workloads): remove backend cluster-access (moved in-pod)`.

---

### Task 4: Deliver the admin kubeconfig to the runner pod

**Files:** `run_service.py`, `pod_launch.py`, tests.

**Interfaces:**
- `run_service.run_workload_job` reads the control-plane node's `data.ocpKubeconfig` from `project.deployed_topology or project.topology` (stored data — **no cluster call**; reuse `deploy_service._stored_cluster_creds` for the read) and passes it to the artifact builder.
- `pod_launch.build_artifact_files(..., kubeconfig: str | None)` adds it at `RunPaths.kubeconfig` (0600). `build_run_command` exports `KUBECONFIG=<paths.kubeconfig>` before the playbooks.

- [ ] TDD: assert the kubeconfig lands in the files map + `KUBECONFIG` is exported in the run command. Implement. `pyright app/` 0/0/0. Commit `feat(workloads): deliver admin kubeconfig to runner pod`.

---

### Task 5: In-pod cluster-admin token mint + `clusters` dict (agnosticd)

**Files:** `pod_launch.py` (`build_run_command`), `run_service.py` (extra-vars), tests. Grounding against `~/agnosticd-v2`.

**Interfaces / behavior:** before running the workloads config, the run command must produce the `clusters: {default: {api_url, api_token}}` dict that `openshift_workload_deployer` consumes (confirmed: `ansible/roles/openshift_workload_deployer` reads `openshift_workload_deployer_clusters[*].{api_url,api_token}`; `configs/openshift-workloads/software.yml` sets it from the `clusters` var).

- [ ] **Step 1 (grounding):** Read `~/agnosticd-v2/ansible/roles/openshift_cluster_admin_service_account/tasks/main.yml` — it mints a cluster-admin SA + binding and publishes `openshift_cluster_admin_token` / `openshift_api_url` via `agnosticd.core.agnosticd_user_info`. Decide the cleanest in-pod wiring: EITHER (a) a tiny prelude play in the run command that runs this role (with `KUBECONFIG` set) then writes `clusters` into the extra-vars the workloads config reads, OR (b) run it as part of the openshift-workloads flow and map its user_data → `clusters`. Prefer the simplest that keeps `openshift-workloads` config unmodified. Cite the role's output vars.
- [ ] **Step 2:** Implement the run-command prelude (mint token in-pod using the delivered `KUBECONFIG`, then set `clusters`/`K8S_AUTH`). Keep `set -euo pipefail`, tee to the logfile. Shell-safe interpolation.
- [ ] **Step 3:** TDD the command shape (mint step present, `clusters` populated, ordered before `main.yml`); mock — no live cluster. `pyright app/` 0/0/0. Commit `feat(workloads): mint cluster-admin token in-pod for workloads`.

---

### Task 6: Frontend — show the control-plane-usable timestamp in Status & Log

**Files:** the recert **Status & Log** panel component in `src/frontend/` (find it: search for "RE-CERTIFIED" / the install-progress panel).

- [ ] Read `ocp_control_plane_usable_elapsed`/`_at` from the project/deploy-progress payload and render a separate line/badge: `minimal control-plane-usable reached at Xm Ys`, distinct from the overall `RE-CERTIFIED · Xm Ys` duration. Match the panel's existing style (PatternFly). `tsc` clean. Commit `feat(ui): show control-plane-usable milestone in recert Status & Log`.

---

### Task 7: Live end-to-end validation

**Not a unit test — a real run** (deploy is resource-heavy; get user OK per their rules).

- [ ] With everything on latest (backend/worker restarted; dal13 operator `:latest`), deploy a throwaway SNO from the "OpenShift 4.22 SNO kv-pattern" pattern; wait for the **control-plane-usable milestone** (Task 1) to flip.
- [ ] POST an ad-hoc run: `POST /api/v1/projects/{id}/workloads` `{"kind":"ad_hoc","role_fqcn":"agnosticd.core_workloads.ocp4_workload_example"}`.
- [ ] Watch the **workload-runner pod** in `troshka-<pid8>`: EE image pulls → clones agnosticd-v2 → `install_dynamic_dependencies` → cluster-admin SA minted in-pod → `main.yml` (openshift-workloads) runs the role against the cluster → `WorkloadRun.status == succeeded`.
- [ ] Destroy the throwaway. Record the outcome.

---

## Self-Review

- Spec coverage: D7 (Tasks 3–5), D12 (no backend cluster calls — Tasks 1/2/3/4 read only persisted/stored data), D13 (Tasks 1/2 gate + Task 6 timestamp). ✔
- Placeholder scan: grounding steps (Tasks 1/5) name the exact files to read and cite; not vague TODOs.
- Cross-file: `RunPaths.kubeconfig` (Task 4) consumed by `build_run_command` (Task 5); `Project.ocp_control_plane_usable_at` (Task 1) read by the gate (Task 2) and the frontend (Task 6).
- ⚠️ **Coordination:** `deploy_service.py` is edited by the MTU work too — rebase onto latest `main` before Task 1 and keep the recert-milestone change surgical.

## Execution Handoff

Plan saved to `docs/superpowers/plans/2026-09-13-troshka-workloads-clusteraccess-rework.md`. In the new session: read this plan + the spec, then use superpowers:subagent-driven-development. The live test cluster and branch state are recorded in auto-memory (`project_workloads-subsystem`).
