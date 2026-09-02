# Multi-Cluster OCP — Plan 4: Ops Pod (Bastionless Install) — Foundation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the foundation for running the OCP agent install from a per-project "ops pod" (modeled on the showroom pod) instead of the bastion VM: a project-scoped API key the pod authenticates with, the ops-pod spec/scaffold, the install-runner script (relocating the bastion's exact steps, fed by Plan 3's stored per-cluster configs), and the deploy-orchestration wiring to create the pod and run + track the install.

**Architecture:** Backend + troshkad-adjacent. The ops pod reuses the showroom netns-transit model (`infra_transit` network → reaches every lab bridge incl. `br-bmc-*`, so it hits per-VM sushy BMCs the same way the bastion does). It consumes `cluster["_generatedInstallConfig"]`/`["_generatedAgentConfig"]` (Plan 3). It authenticates back to the Troshka API with a **project-scoped, limited-permission** API key (new). Install execution (agent-ISO build, HTTP serve, Redfish loop, wait-for) is inherently **live-env-only**; this plan makes every *generation/shaping/wiring* piece unit-tested and marks execution for a documented manual/integration checklist. **Bastion removal, the execution-environment image build (CI), KubeVirt-provider parity, and health-monitor migration are Plan 4b** — the bastion stays in place and functional through Plan 4.

**Tech Stack:** Python 3.11 (CI 3.13), FastAPI, SQLAlchemy 2, Alembic, pytest (SQLite), RQ/Redis (+ daemon-thread fallback). Pod runtime: podman via troshkad. No new backend deps.

**Spec:** `docs/superpowers/specs/2026-09-01-multi-cluster-ocp-and-bastionless-install-design.md` (§7 Ops pod; §9 scoped auth; decisions #7, #8). **Predecessors:** Plans 1-3 complete on this branch.

## Global Constraints

- **Non-breaking:** the bastion install path stays fully functional through Plan 4 (removal is Plan 4b). Full suite stays green. A full-access (unscoped) API key must behave EXACTLY as today.
- **Scoped key is least-privilege:** project-bound + perms `["topology:read", "vm:exec"]` only; cannot touch other projects; auto-revoked on project/pod destroy.
- **Reuse, don't reinvent:** ops-pod networking = the showroom `infra_transit` model (`showroom_infra_network`/`_pod_create_params`/`_attach_pod_to_infra_transit`); install steps = the bastion's exact sequence (`_build_install_script`), relocated verbatim in behavior.
- **Per-cluster:** the runner installs each cluster from its own `_generatedInstallConfig`/`_generatedAgentConfig` + that cluster's BMC IPs/password; multi-cluster runs in parallel.
- **Live-env-only tasks are explicitly labeled** — they ship unit-tested generation/wiring + a documented checklist; actual pod/netns/Redfish/install runs are verified in the user's environment.
- FK columns use `postgresql.UUID(as_uuid=False)`; Alembic migration runs on startup (never manually). Cognitive complexity ≤ 15. `black` + `pyright` clean. Full suite: `cd src/backend && ./venv/bin/python3 -m pytest`. Git via absolute paths; NO Co-Authored-By.
- **PROCESS GUARD (STRICT, all implementers):** NEVER `git stash`/`stash pop`/`stash apply` — not even during investigation (three prior agents violated this). Only `git add <specific paths>` + `git commit`. If `git status` shows unexpected files, STOP and report.

## Codebase map (verified)

- **ApiKey:** `models/api_key.py:27` (cols id/user_id/name/key_hash/key_prefix/is_active/last_used_at/expires_at/created_at; `generate_api_key()`:19 → `trk_...`; `hash_key()`:23). **No scope column.** Create route `api_keys.py:52`. Auth `_get_user_from_api_key` `core/auth.py:233` (returns `api_key.user`, discards key), `get_current_user:281`, `require_role:322`. Enforcement points: `get_project` `api/projects.py:1037` (owner/admin check), `vm_exec` `api/projects.py:2853`.
- **Pod create:** troshkad `/commands/pods/create` → `_handle_pod_create` `troshkad.py:11942` (params pod_name/project_id/networks/init_containers/containers/volumes/restart_policy/privileged); `infra_transit` net → `_attach_pod_to_infra_transit:11573` + `_allow_infra_veth_forward:11728` (masquerade to all `br-*` incl `br-bmc-*`) + `_write_pod_resolv_conf:11874`. Destroy `_handle_pod_destroy:12097`.
- **Backend pod orchestration:** `_pod_create_params` `deploy_service.py:1666`; `_create_pod` `deploy_service.py:1769` (`start_job(host,"/pods/create",...)`); `showroom_infra_network` `deploy_topology.py:605`; `_find_container_networks` `deploy_topology.py:822`. Deploy steps `DEPLOY_STEPS` `deploy_service.py:78`; `_deploy_single_host_execute:4909` (containers at `:4952`, start at `:4963`, after VMs up). Progress: `set_progress`/`get_progress`/`mark_cancelled` `core/redis.py:304+`; `_update_deploy_progress` `deploy_service.py:153`; `enqueue_job` `core/redis.py:204`.
- **Bastion install (to relocate):** `agent_template.py` `_build_install_script:1542` (download tools → `agent create image` → `http.server 8080` → Redfish InsertMedia/Reset loop → `wait-for install-complete` → eject), `_collect_bmc_ips_and_password:798`, `_write_ocp_config_files:822`, `_setup_bastion_auto_install:879`. Per-cluster configs stored `_customize_one_cluster` `agent_template.py:454` (`cluster["_generatedInstallConfig"]`:468, `["_generatedAgentConfig"]`:476).
- **Tests:** troshkad pod handlers `src/troshkad/tests/test_troshkad.py:1918+` (mock `_run_cmd`); backend `test_deploy_orchestration.py:3748` (`_create_and_start_pod`); `test_agent_template_multicluster.py`.
- **LIVE-ENV-ONLY (verified not unit-testable):** `_attach_pod_to_infra_transit`/`_allow_infra_veth_forward` (netns/veth/nft), real Redfish InsertMedia/Reset/Eject vs sushy, `python3 -m http.server` ISO serve, `openshift-install agent create image` / `wait-for install-complete`.

---

### Task 1: Scoped `ApiKey` model (project + permissions)

**Files:** Modify `src/backend/app/models/api_key.py`; create Alembic migration in `src/backend/alembic/versions/`; Test `src/backend/tests/test_scoped_api_key.py` (new).

**Interfaces:**
- Produces: `ApiKey` gains `project_id: Mapped[str | None]` (`postgresql.UUID(as_uuid=False)`, nullable FK → projects.id, `ondelete="CASCADE"`) and `scopes: Mapped[list[str] | None]` (JSON/JSONB, nullable). `None`/empty on both = a full-access user key (today's behavior). A helper `ApiKey.has_scope(perm: str) -> bool` and `ApiKey.is_scoped -> bool` (true when project_id set).

- [ ] **Step 1: Write failing test** — construct an `ApiKey` with `project_id="p1"`, `scopes=["topology:read"]`; assert `is_scoped` True, `has_scope("topology:read")` True, `has_scope("vm:exec")` False; an unscoped key (`project_id=None`) → `is_scoped` False, `has_scope(anything)` semantics = full access (define `has_scope` to return True when unscoped). Model tests use the SQLite test session.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** columns (follow SQLAlchemy 2 `Mapped`/`mapped_column`, register nothing new in `__init__` — same model file) + helpers + the Alembic migration (`add_column` both, nullable; FK with `postgresql.UUID(as_uuid=False)`). Chain the migration off the current head.
- [ ] **Step 4: Run migration + tests** — `cd src/backend && ./venv/bin/python3 -m alembic upgrade head` (against a scratch/sqlite as tests do) then the new test → pass. Existing api-key tests stay green.
- [ ] **Step 5: Commit** (`feat(auth): project-scoped, limited-permission API keys (model+migration)`).

---

### Task 2: Enforce scope in auth

**Files:** Modify `src/backend/app/core/auth.py` (`_get_user_from_api_key`, add dependency); Modify `src/backend/app/api/projects.py` (`get_project`, `vm_exec` enforcement); Test `test_scoped_api_key.py`.

**Interfaces:**
- Produces: `_get_user_from_api_key` stashes the matched `ApiKey` on `request.state.api_key` (or returns it) so downstream can read scope. A dependency `enforce_project_scope(perm: str)` (factory) that: if the request authenticated via a SCOPED key, requires the key's `project_id` == the route's `{project_id}` AND `key.has_scope(perm)`, else 403; if authenticated via an unscoped key/JWT/dev, no-op (existing owner/role checks apply). Wire `topology:read` onto `get_project` and `vm:exec` onto `vm_exec`.

- [ ] **Step 1: Write failing tests** (dev-mode auth makes this fiddly — test the enforcement helper directly + an integration test injecting a scoped key via the `Authorization: Bearer trk_...` header):
  - a scoped key for project A can `get_project(A)` and `vm_exec(A,...)` but gets 403 on `get_project(B)` and on a project-A route requiring a perm it lacks;
  - an unscoped key behaves exactly as today (full access).
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** — surface the key in `_get_user_from_api_key`; add `enforce_project_scope`; wire it into the two routes WITHOUT breaking the existing owner/admin/dev paths. Keep complexity ≤ 15.
- [ ] **Step 4: Run → pass** (+ existing `test_api_projects.py` auth tests green).
- [ ] **Step 5: Commit** (`feat(auth): enforce project scope + permissions for scoped API keys`).

---

### Task 3: Mint / revoke the ops-pod key

**Files:** Create `src/backend/app/services/ocp/ops_pod_auth.py`; Test `test_scoped_api_key.py`.

**Interfaces:**
- Produces: `mint_ops_pod_key(db, project) -> str` — creates (or rotates) an `ApiKey` owned by the project owner, `project_id=project.id`, `scopes=["topology:read","vm:exec"]`, name `ops-pod:{project.id}`, returns the raw `trk_...` once. `revoke_ops_pod_key(db, project_id)` — deactivates/deletes the ops-pod key(s) for the project. Idempotent mint (revoke-then-create or reuse).

- [ ] **Step 1: Write failing test** — `mint_ops_pod_key` returns a `trk_` key; the stored key has `project_id`, the two scopes, is_active; `revoke_ops_pod_key` deactivates it; minting twice doesn't leave two active ops-pod keys.
- [ ] **Step 2: Run → fail.** **Step 3: Implement.** **Step 4: Run → pass.**
- [ ] **Step 5: Commit** (`feat(ocp): mint/revoke project-scoped ops-pod API key`).

---

### Task 4: Ops-pod network + spec builder

**Files:** Create `src/backend/app/services/ocp/ops_pod_scaffold.py`; possibly extend `deploy_topology.py` (an `ops_pod_infra_network` mirroring `showroom_infra_network`); Test `src/backend/tests/test_ops_pod.py` (new).

**Interfaces:**
- Produces:
  - `ops_pod_infra_network(vni_map, mac, dns_nameserver) -> dict` — an `infra_transit` network dict like `showroom_infra_network` but a distinct pod IP (e.g. `172.30.{octet3}.4`) so it can coexist with the showroom pod.
  - `build_ops_pod_config(project, clusters, api_url, api_key, ocp_version, pull_secret_json) -> dict` — the pod scaffold consumed by `_pod_create_params`-style shaping: `pod_name="ops"`, one main container using the EE image (Task 8/Plan 4b provides the real image ref — use a config-driven constant `OPS_POD_IMAGE`), `env` = `TROSHKA_API_URL/TROSHKA_API_KEY/TROSHKA_PROJECT_ID/OCP_VERSION`, `mounts`/`volumes` carrying each cluster's `_generatedInstallConfig`/`_generatedAgentConfig` + the pull secret (written to a per-project workdir the pod mounts), `privileged` as needed for the install, `restart_policy="always"` (persistent ops pod per spec §7).

- [ ] **Step 1: Write failing tests** — `ops_pod_infra_network` returns `infra_transit:True` with a distinct IP from showroom's `.3` (e.g. `.4`); `build_ops_pod_config` produces a pod dict with the EE image, the 4 env vars (api url/key/project/version), and per-cluster config material for a 2-cluster project (both clusters' install-config/agent-config present), pull secret present, restart_policy always.
- [ ] **Step 2: Run → fail.** **Step 3: Implement.** **Step 4: Run → pass.**
- [ ] **Step 5: Commit** (`feat(ocp): ops-pod network + spec builder`).

---

### Task 5: Install-runner script generator

**Files:** Modify `agent_template.py` (extract the install steps) or create `src/backend/app/services/ocp/ops_pod_install.py`; Test `test_ops_pod.py`.

**Interfaces:**
- Produces: `build_ops_pod_install_script(clusters, bmc_by_cluster, ocp_version, workdir) -> str` — a pod-runnable script that, PER CLUSTER (in parallel via backgrounded shell or an ansible loop), reproduces the bastion's exact steps against that cluster's own config + BMCs: ensure `oc`/`openshift-install` present (baked in EE, else download — reuse the version URL logic), write install-config/agent-config into `<workdir>/<clusterId>/`, `openshift-install agent create image`, serve the ISO (HTTP), Redfish `InsertMedia`+`ComputerSystem.Reset` loop over that cluster's BMC IPs (auth `admin:<bmcPassword>`), `agent wait-for install-complete`, eject. `bmc_by_cluster` = `{clusterId: (bmc_ips, bmc_password)}` from `_collect_bmc_ips_and_password` scoped per cluster (extend that helper or add a per-cluster variant using `cluster_member_nodes`). Reuse the exact Redfish/serve/wait-for command strings from `_build_install_script`.

- [ ] **Step 1: Write failing tests** — for a 2-cluster input, the generated script contains: per-cluster workdirs, `agent create image` per cluster, a Redfish `InsertMedia` call per cluster targeting that cluster's BMC IPs + password, `wait-for install-complete` per cluster, and parallel execution (both clusters' blocks backgrounded / looped). Assert the script references each cluster's `_generatedInstallConfig`. (Assert on generated text; execution is live-env.)
- [ ] **Step 2: Run → fail.** **Step 3: Implement** — factor the shared step strings out of `_build_install_script` (keep the bastion using them unchanged — extract to helpers, don't rewrite bastion behavior) so bastion + ops-pod share one source of truth for the Redfish/serve/wait-for commands. **Step 4: Run → pass** (+ `test_agent_template*` green — bastion script unchanged).
- [ ] **Step 5: Commit** (`feat(ocp): per-cluster ops-pod install-runner script`).

---

### Task 6: Deploy-orchestration — create the ops pod (wiring)

**Files:** Modify `deploy_service.py` (`_pod_create_params` handles the ops pod; insert ops-pod creation after the `starting` step for OCP projects); Test `test_deploy_orchestration.py` / `test_ops_pod.py`.

**Interfaces:**
- Consumes: Tasks 3-5.
- Produces: for an OCP project with `topology["clusters"]`, after VMs are started, the deploy flow mints the scoped key (Task 3), builds the ops-pod config (Task 4) + install script (Task 5), and issues `start_job(host, "/pods/create", <ops-pod params>)` then `/pods/start`. **Unit-test the params/sequence with a mocked troshkad client** (mirror `test_deploy_orchestration.py:3748`'s `_create_and_start_pod` test). Behind a config/feature flag `ocp_install_via_pod` (default False so the bastion path stays default until Plan 4b) so single-cluster keeps using the bastion; when the flag is on OR the project is multi-cluster, use the ops pod. **[LIVE-ENV]** the actual pod run is not unit-tested.

- [ ] **Step 1: Write failing test** — with a mocked host/troshkad client and the flag on (or a 2-cluster project), deploy issues `/pods/create` with the ops-pod params (name `ops`, EE image, scoped-key env, per-cluster configs) then `/pods/start`; with the flag off + single cluster, the bastion path is used (no ops pod). 
- [ ] **Step 2: Run → fail.** **Step 3: Implement** the wiring + flag; keep `_deploy_single_host_execute` under complexity 15 (delegate to an `_deploy_ops_pod(...)` helper). **Step 4: Run → pass.**
- [ ] **Step 5: Commit** (`feat(ocp): create ops pod during deploy (flagged)`).

---

### Task 7: Install progress + cancellable job

**Files:** Modify `deploy_service.py` (ops-pod install monitor); Test `test_ops_pod.py`.

**Interfaces:**
- Produces: an `_monitor_ops_pod_install(project_id, ...)` background routine that polls the ops pod's per-cluster install progress (tail logs via troshkad exec / read a status file) and reports via `set_progress`/`_update_deploy_progress` with per-cluster status, honoring `is_cancelled` (→ troshkad `DELETE /jobs/{job_id}` / stop). **Unit-test the state machine** (given mocked log/status inputs → correct progress transitions + cancellation), mirroring the existing `_monitor_ocp_vm_health` pattern. **[LIVE-ENV]** real log tailing/timing.

- [ ] **Step 1: Write failing test** — feed mocked per-cluster status inputs; assert `set_progress` reflects per-cluster install phases (creating-image / booting / waiting / complete / failed) and that a cancel signal stops the monitor. **Step 2: fail. Step 3: implement. Step 4: pass.**
- [ ] **Step 5: Commit** (`feat(ocp): ops-pod install progress + cancellation`).

---

### Task 8: Execution-environment image spec (Dockerfile; CI builds)

**Files:** Create `src/backend/../images/ops-pod/Dockerfile` (locate the repo's image dir convention first — check `docs/dev/deployment.md` / existing Dockerfiles) + a short README; reference `OPS_POD_IMAGE` config default.

**Interfaces:**
- Produces: a Dockerfile for the ops-pod EE containing `oc` + `openshift-install` (fetched at build), the `troshka.cloud` Ansible collection, `ansible-core`/`ansible-navigator`, and Redfish/curl tooling. **Do NOT build locally** (CI builds per project convention). Wire `OPS_POD_IMAGE` into config (`config.yaml`) with a placeholder registry path.

- [ ] **Step 1:** Locate the image/build convention (existing Dockerfiles, CI image jobs) and the config pattern for image refs. **Step 2:** Write the Dockerfile + README + `OPS_POD_IMAGE` config default. **Step 3:** Sanity-check (hadolint if available / manual review); do NOT `podman build`. **Step 4:** Add a test asserting `OPS_POD_IMAGE` config resolves + `build_ops_pod_config` uses it. **Step 5: Commit** (`feat(ocp): ops-pod execution-environment image spec (CI-built)`).

---

### Task 9: Regression + live-env verification checklist

- [ ] **Step 1:** Full suite `cd src/backend && ./venv/bin/python3 -m pytest -q` → green (twice; note known flakes). `pyright` clean on changed modules.
- [ ] **Step 2:** Confirm the bastion path is UNCHANGED with the flag off (existing OCP deploy tests green; single-cluster still bastion).
- [ ] **Step 3:** Produce a **live-env verification checklist** (in the report) for the user's environment: deploy a 2-cluster project with `ocp_install_via_pod` on → ops pod is created (netns-attached, reaches `br-bmc-*`), mints+uses the scoped key (verify it can read topology + exec but NOT touch another project), builds per-cluster agent ISOs, drives Redfish against each cluster's BMCs, both clusters reach install-complete, progress reports per-cluster, cancel works. Note: requires a real host + sushy + the EE image published by CI.
- [ ] **Step 4: Commit** any fixes (`test(ocp): ops-pod foundation regression`).

---

## Self-Review

**Spec coverage (§7, §9):**
- Scoped API key (project + perms), minted/revoked for the pod → Tasks 1-3. ✓
- Ops pod modeled on showroom (netns transit, reaches BMCs), consumes per-cluster configs → Tasks 4-5. ✓
- Per-cluster parallel install (ISO build + Redfish + wait-for) relocated from bastion → Task 5 (generation), live-run deferred. ✓
- Deploy wiring to create + run + track the pod → Tasks 6-7 (wiring/state unit-tested; live run checklisted). ✓
- EE image → Task 8 (spec; CI builds). ✓
- **Deferred to Plan 4b:** bastion removal (templates + DNS + VNC/console + `bastions,showroom` + health-monitor migration), KubeVirt-provider ops-pod parity, and making `ocp_install_via_pod` the default / removing the bastion path. Console→showroom terminal is Plan 5.

**Placeholder scan:** `OPS_POD_IMAGE` is an intentional config placeholder (real image = Task 8 / CI). Live-env steps are labeled `[LIVE-ENV]` with unit-tested generation/wiring substitutes — not TBDs.

**Type/interface consistency:** scoped-key helpers (`is_scoped`/`has_scope`) used consistently across Tasks 1-3; `_generatedInstallConfig`/`_generatedAgentConfig` (Plan 3) consumed by Tasks 4-5; `infra_transit` net shape matches `showroom_infra_network`; `_pod_create_params` reused for the ops pod in Task 6.

## Roadmap
- **Plan 4b** — bastion removal + EE image CI build + KubeVirt-provider ops-pod parity + health-monitor/DNS/console migration + flip `ocp_install_via_pod` default.
- **Plan 5** — console pod + showroom terminal (lab-user oc shell). **Plan 6** — inventory plugin per-cluster groups + day-2/monitoring.
