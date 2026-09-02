# Multi-Cluster OCP — Plan 4b: Install-Method Choice (pod default) + Pod-Path Hardening

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make OCP install method a **per-project choice — `install_via: bastion | pod`, defaulting to `pod`** — WITHOUT removing the bastion (bastion stays as the opt-in alternative), and make the pod install path production-ready (spawn the install monitor with dead-job detection, idempotent re-runs, cancellation that actually stops the pod, secrets via mounted files instead of argv, and monitoring/status for pod projects that have no bastion VM).

**Architecture:** Backend + a new troshkad file-mount capability + a KubeVirt ops-pod path + one frontend toggle. The bastion and pod paths already coexist at a single deploy branch (`_should_use_ops_pod`); Plan 4b converts the global `install_via_pod` flag into a per-project `install_via` selector (default `pod`), keyed off the project/topology rather than a global config. The bastion path stays byte-identical for `install_via: bastion`. **The pod path works on BOTH troshkad/libvirt (podman netns-transit) AND KubeVirt (a namespace pod attached to the cluster + BMC NADs, mirroring the operator's sushy Deployment)** — so pod-default applies to all host types (no KubeVirt→bastion fallback).

**Tech Stack:** Python 3.11 (CI 3.13), FastAPI, pytest; troshkad (podman); Next.js frontend. No new backend deps.

**Spec:** `docs/superpowers/specs/2026-09-01-multi-cluster-ocp-and-bastionless-install-design.md` (§6/§7 — reinterpreted: pod is the default install *runner*, bastion is a retained option rather than removed). **Predecessors:** Plans 1-4 complete on this branch.

## Decisions (settled with the user)
- **Keep the bastion** — do NOT remove it or migrate its DNS/VNC/console/monitor. It remains a selectable install method.
- **Install method is per-project:** `install_via: bastion | pod`. **Default = `pod`.**
- **Sequencing:** harden the pod path FIRST; flip the default to `pod` LAST (so a default deploy never runs an incomplete path).
- **KubeVirt hosts:** pod path works on KubeVirt too (in-scope for 4b) — a namespace pod attached to the cluster + BMC NADs. No bastion fallback.
- **Secrets:** delivered to the pod via a new troshkad file-mount capability (host dir 0600 + read-only bind-mount) / a k8s Secret on KubeVirt — NOT base64-in-argv.
- **Caveat (explicit):** the real pod/Redfish/install run is not exercisable in the authoring session — pod-default is verified at unit/wiring level here; the bastion opt-in + Task N live-env checklist are the safety nets before production trust.

## Global Constraints
- **Bastion path byte-identical** for `install_via: bastion` (existing `_build_install_script` golden + cloud-init tests stay green).
- **Distinct schema key:** use `install_via` (NOT `install_method`, which already means the agent installer type — `template_loader.py:497/527`).
- Non-breaking: full suite green; per-project default resolution is deterministic.
- Cognitive complexity ≤ 15; `black` + `pyright` clean; FK/JSONB conventions.
- **PROCESS GUARD (all implementers):** NEVER `git stash`/`stash pop`/`stash apply`; only `git add <paths>` + commit; STOP+report on unexpected `git status`. (Enforced after 3 prior incidents.)
- Run: `cd src/backend && ./venv/bin/python3 -m pytest`. Live-env tasks ship unit-tested generation/wiring + a documented checklist; mark them **[LIVE-ENV]**.

## Codebase map (verified — Plan 4b targets)
- Selector: `deploy_service.py` `_ocp_install_via_pod():1803` (global flag), `_should_use_ops_pod(topology):1825`, branch at `:5307`. Bake decision: `agent_template.py customize_topology:519` (keyed on `len(clusters)==1` at `:553` — WRONG proxy; should key on `install_via`), `_bake_single_cluster_bastion:489`. Config plumbing: `projects.py create_project_from_template:629`, config dict `:699-714`.
- Monitor: `_monitor_ops_pod_install:2079` (NOT spawned; grep-confirmed), `_deploy_ops_pod:1951` (returns without spawning `:1980-1982`), dead-job gap (`_read_ops_pod_cluster_logs:2041` never injects `"failed"`; state machine `ops_pod_install.py:121` accepts `"failed"` via `_phase_from_input:96`), cancel gap (`_cancel_ops_pod_install:2058` → `cancel_job` on the completed `/pods/create` job).
- Idempotency: bastion guard `agent_template.py:1647` (`if [ -f .../auth/kubeconfig ]; exit 0`); ops-pod script `ops_pod_install.py _cluster_install_block:218` has NO guard (+ `restart_policy=always` `deploy_service.py:1946` → re-runs on restart).
- Secrets: `_ops_pod_workdir_lines:1840` base64-embeds configs/pull-secret in argv (`echo <b64> | base64 -d > path`); `TROSHKA_API_KEY` in env `:1931`. troshkad `_handle_pod_create:11942` handles only networks/init_containers/containers/volumes/restart_policy/privileged — **no file-write mechanism**; `_create_main_container:11902` bind-mounts `ctr["mounts"]` (needs pre-existing host path). Dead Task-4 `build_ops_pod_config` (`ops_pod_scaffold.py:95`) returns an unused `files`/`mounts` shape.
- Monitor flags: `ocp_topology_flags.py has_bastion_vm:17`, `apply_sno_ocp_vm_flags:28` sets `ocpMonitor`/`configureBastionBrowser` ONLY when a bastion VM exists (`:43`) — pod projects (no bastion node) get no monitor flags.
- KubeVirt: pod path is troshkad-only (`_deploy_ops_pod` no kubevirt branch); kubevirt sushy = `operator/helpers/bmc.py build_bmc_deployment` (NAD-attached Deployment on :8000); provider checks `host.host_type=="kubevirt-cluster"`.
- Frontend: create dialog `frontend/src/app/projects/page.tsx:209-221` (posts auto_install_ocp/bastion_*); canvas `PropertiesPanel.tsx:1716-1740` (ocpMonitor/configureBastionBrowser).
- Tests: `test_ops_pod.py` (scaffold/script/state — not the deploy-side params), `test_deploy_orchestration.py:4022` (selector), `test_agent_template*.py` (bastion golden), `test_ocp_topology_flags.py:30`.

---

### Task 1: `install_via` schema + config default = pod
**Files:** `template_loader.py` (normalize_ocp_section / resolve), `projects.py:699-714`, `config.yaml`; Test `test_ocp_clusters.py` / `test_deploy_template.py`.
- Add a per-PROJECT `install_via: "bastion"|"pod"` (default `"pod"`) resolved from template/body; distinct from the existing `install_method` (agent type). Config `ocp.install_via_default: "pod"` replaces the old boolean `ocp.install_via_pod` (keep reading the old flag as a fallback for one release). `create_project_from_template` puts `install_via` in the config dict + persists it where `customize_topology` and deploy can read it (topology-level, e.g. `topology["ocpInstallVia"]`, or project column — pick the one both customize-time and deploy-time can see; topology is simplest since deploy reads topology).
- [ ] Tests: default (no field) → `pod`; explicit `bastion`/`pod` honored; legacy `install_via_pod: true` config still yields pod. TDD. Commit.

### Task 2: `_should_use_ops_pod` = per-project (all host types)
**Files:** `deploy_service.py:1803/1825`; Test `test_deploy_orchestration.py`.
- `_should_use_ops_pod(topology)` returns True when the project's `install_via == "pod"` (default), False for `bastion` — on ALL host types (troshkad + kubevirt; the kubevirt pod path is Tasks 8b-8d). Drop the global-flag-only read; keep config as the default source when the project doesn't specify.
- [ ] Tests (rewrite the selector tests): pod-project→True; bastion-project→False; unset→True (pod default). TDD. Commit.

### Task 3: `customize_topology` bakes bastion only for `install_via: bastion`
**Files:** `agent_template.py:519/553/489`; Test `test_agent_template*.py`.
- Replace the `len(clusters)==1` bake heuristic with `install_via == "bastion"` → `_bake_single_cluster_bastion` (still single-cluster only for bastion; a bastion can't run multi-cluster — if `install_via==bastion` AND multi-cluster, raise a clear validation error). For `install_via == "pod"`: never bake the bastion; always store `_generatedInstallConfig`/`_generatedAgentConfig` on every cluster (resolves the Plan-4 "multi-cluster + flag-off = no installer" inconsistency). Bastion output byte-identical for the bastion path.
- [ ] Tests: bastion single-cluster → cloud-init baked (unchanged golden); pod any-cluster → no bake + `_generated*` on all clusters; bastion+multi-cluster → validation error. TDD. Commit.

### Task 4: Idempotency skip-guard in the ops-pod install script
**Files:** `ops_pod_install.py:218/265`; Test `test_ops_pod.py`.
- In `_cluster_install_block`, before `agent create image`, emit the bastion's guard equivalent: `if [ -f <workdir>/<clusterId>/auth/kubeconfig ]; then echo "[<id>] already installed, skipping"; exit 0; fi` (per-cluster, so a pod restart under `restart_policy=always` doesn't re-run completed clusters).
- [ ] Test: generated script contains the per-cluster kubeconfig-exists skip guard before create-image. Bastion golden unaffected. TDD. Commit.

### Task 5: Spawn the monitor + feed dead-job → failed
**Files:** `deploy_service.py` (`_deploy_ops_pod:1951`, `_monitor_ops_pod_install:2079`, `_read_ops_pod_cluster_logs:2041`); Test `test_deploy_orchestration.py`/`test_ops_pod.py`.
- `_deploy_ops_pod` spawns `_monitor_ops_pod_install` (daemon thread / enqueue, mirroring `maybe_start_ocp_health_monitor`) after create+start. **[LIVE-ENV loop]**
- Feed a dead-job/pod-not-running signal into the state machine: if the ops pod/container is not running (troshkad status) and a cluster's log isn't terminal, inject `{cid: "failed"}` into `ops_pod_install_progress` so a crashed install reports `failed` instead of spinning to the 2h timeout. Unit-test the injection decision (given "pod not running" + non-terminal log → failed).
- [ ] Tests: monitor spawned in `_deploy_ops_pod` (mocked); dead-job injection → failed (pure). TDD. Commit.

### Task 6: Cancellation stops the pod
**Files:** `deploy_service.py:2058`; troshkad `/pods/destroy` or `/pods/stop` (confirm handler `_handle_pod_destroy:12097`/`_handle_pod_start`); Test.
- `_cancel_ops_pod_install` must stop/delete the ops pod (troshkad `/pods/destroy` for `troshka-<id8>-ops`), not `cancel_job` on the completed `/pods/create` job. Unit-test the param/call shaping; **[LIVE-ENV]** actual stop.
- [ ] Test: cancel issues `/pods/destroy` for the ops pod name (mocked). TDD. Commit.

### Task 7: Monitoring + `ocp_status` for pod projects (no bastion VM)
**Files:** `deploy_service.py` (monitor status wiring), `ocp_topology_flags.py:28`; Test `test_ocp_topology_flags.py`.
- Pod projects have no bastion/`ocpMonitor` node, so the existing `maybe_start_ocp_health_monitor` gate never fires. Wire the pod path to set `project.ocp_status`/`ocp_install_elapsed` via the same `_ocp_update_status` fields the UI reads (from `_monitor_ops_pod_install`), and give pod projects an equivalent "monitoring" trigger. Decide + implement: pod install status flows through `ocp_status` (reuse existing UI) rather than only deploy-progress.
- [ ] Tests: pod-project install phases update `ocp_status`/elapsed; `apply_sno_ocp_vm_flags` no longer assumes a bastion for pod projects. TDD. Commit.

### Task 8: Secrets via mounted files (new troshkad file capability)
**Files:** troshkad `_handle_pod_create` (+ a `files` handler writing to a host dir + bind-mount), `deploy_service.py _ops_pod_create_params/_ops_pod_workdir_lines`; reconcile dead `ops_pod_scaffold.build_ops_pod_config`; Tests `test_troshkad.py`, `test_ops_pod.py`.
- Add a troshkad `/pods/create` `files: {path: content}` capability: troshkad writes each file to a per-pod host dir (0600) and bind-mounts it read-only into the container — so install-config/agent-config/pull-secret/API-key are NOT in the argv/`podman inspect`. Switch `_ops_pod_create_params` to use `files`+mounts instead of base64-in-command; keep `TROSHKA_API_KEY` out of argv (env is acceptable per §7, or also a mounted file). **[LIVE-ENV mount; UNIT handler logic + param shaping]**
- [ ] Tests: troshkad `files` handler writes+mounts (unit, mocked fs); `_ops_pod_create_params` emits `files`+mounts, no secret in `command`. TDD. Commit. (If the troshkad file handler proves too large, split into its own task.)

### Task 8b: KubeVirt ops-pod — spec/creation + provider branch
**Files:** `deploy_service.py` (`_deploy_ops_pod:1951` add kubevirt branch), `providers/kubevirt.py` (a `create_ops_pod`), possibly `operator/` (if the operator must materialize it); Test `test_ops_pod.py`/`test_kubevirt_provider.py`.
- On a `kubevirt-cluster` host, `_deploy_ops_pod` creates a Pod in `project-{pid8}` (via k8s API or operator CR) using `OPS_POD_IMAGE`, attached via `k8s.v1.cni.cncf.io/networks` to BOTH the cluster NAD (reach nested VMs / serve ISO) and the BMC NAD (reach sushy `:8000`), privileged + NET_ADMIN/NET_RAW — mirror `operator/helpers/bmc.py build_bmc_deployment`. Env carries the scoped key/API url/project; per-cluster configs + pull-secret delivered via a **k8s Secret** mounted into the pod (the KubeVirt analog of Task 8's file-mount). Reuse the same install script (`build_ops_pod_install_script`) as command.
- [ ] Tests: kubevirt branch builds a Pod/CR spec with both NAD annotations, the EE image, secret-mount (not argv), and the install-script command; the troshkad branch is unchanged. **[LIVE-ENV]** actual pod run + NAD reachability. TDD. Commit.

### Task 8c: KubeVirt ops-pod — exec/log-read, monitor, cancel
**Files:** `deploy_service.py` (`_read_ops_pod_cluster_logs:2041`, `_monitor_ops_pod_install:2079`, `_cancel_ops_pod_install:2058`); Test.
- Make the log-read/monitor/cancel provider-aware: on kubevirt, read per-cluster `install.log` via `connect_get_namespaced_pod_exec` (like `_pod_exec_raw`/`_exec_on_bastion_kubevirt`) instead of troshkad `/containers/exec`; cancel = delete the k8s pod (not troshkad `/pods/destroy`). The pure state machine + progress publishing are shared. **[LIVE-ENV]** exec/timing.
- [ ] Tests: kubevirt log-read + cancel take the k8s path (mocked k8s client); troshkad path unchanged. TDD. Commit.

### Task 9: Frontend `install_via` toggle
**Files:** `frontend/src/app/projects/page.tsx:209-221` (+ maybe `PropertiesPanel.tsx`); Test (Vitest).
- Add an "Install method" select (Pod (default) / Bastion) to the OCP create flow, posting `install_via`. Default pod. (Bastion-specific fields — image/iso/bmc — shown only when bastion.)
- [ ] Test: dialog posts `install_via: "pod"` by default; selecting Bastion posts `bastion` + shows bastion fields. TDD. Commit.

### Task 10: Deploy-side param tests (fill the Plan-4 gap)
**Files:** Test `test_ops_pod.py`/`test_deploy_orchestration.py`.
- Add the missing unit tests for `_ops_pod_create_params`, `_ops_pod_command`, `_deploy_ops_pod` (mint→create→start→spawn-monitor), `_cancel_ops_pod_install`, `_read_ops_pod_cluster_logs` — none are currently tested. Commit.

### Task 11: Flip default to pod + regression + [LIVE-ENV] checklist
**Files:** config default already pod (Task 1); verify end-to-end selection; Test + report.
- [ ] Confirm default resolution = pod (troshkad) / bastion (kubevirt) end-to-end. Full suite green x2. pyright clean. Bastion path golden unchanged.
- [ ] Produce the updated **[LIVE-ENV] checklist**: with a real troshkad host + published EE image, deploy a default (pod) OCP project → ops pod installs per-cluster, monitor reports phases + failure, cancel stops the pod, restart skips completed clusters (idempotency), secrets not in `podman inspect`, scoped key revoked on destroy; and a `install_via: bastion` project still installs via the bastion unchanged.
- Commit.

## Self-Review (to complete after drafting tasks)
- Covers: per-project `install_via` (T1) + selector w/ kubevirt fallback (T2) + bake decision (T3) + idempotency (T4) + monitor spawn/dead-job (T5) + cancel-pod (T6) + pod-project monitoring/status (T7) + secret-mount (T8) + frontend (T9) + deploy-param tests (T10) + default flip/regression/checklist (T11). Bastion retained + byte-identical throughout.
- KubeVirt ops-pod parity is now IN Plan 4b (Tasks 8b-8c). **Plan 5:** console pod + showroom terminal. **Plan 6:** inventory per-cluster + day-2. Carry-forwards from Plan 3 (replicas-vs-hosts guard, rendezvous-is-a-host, single-cluster _generated* double-build) — fold into T3/T5 where they touch the same code.

## Roadmap
- **Plan 5** — console pod + showroom terminal (lab-user oc shell). **Plan 6** — inventory plugin per-cluster groups + day-2/monitoring.
