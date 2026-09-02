# Live-Environment Verification Harness — Design Spec

**Date:** 2026-09-02
**Status:** Approved (design), pending spec review → implementation plan
**Branch context:** verifies the Plan 4b pod-default OCP install (`feat/multi-cluster-ocp-bastionless`), but the harness itself is general-purpose and belongs on its own track.

## 1. Motivation

Plan 4b's OCP **pod-install** path is verified only at unit/wiring level — every test mocks troshkad, the k8s API, Redfish, and the real `openshift-install` run. No authoring session ever boots a real RHCOS node or drives a real BMC. The manual gate that closes this gap is `docs/superpowers/plans/2026-09-02-multi-cluster-ocp-plan4b-live-env-checklist.md`. This spec turns that checklist into an **automated pytest harness** that drives a REAL running Troshka instance and asserts the pod-install behavior on both a troshkad/libvirt host and a kubevirt-cluster host.

## 2. Goals / Non-goals

**Goals**
- Automate the Plan 4b [LIVE-ENV] checklist (Sections A troshkad, B kubevirt, C bastion regression).
- Two tiers: **Tier 1** fast behavioral/security assertions (no OCP install, minutes), **Tier 2** real end-to-end install (SNO, ~30–60 min).
- Zero impact on the existing unit-test CI; the live suite skips unless explicitly configured.
- Add **no production surface** — observe the system exactly as deployed (API + host-ssh + oc + DB), not via test-only endpoints.

**Non-goals**
- No new backend/API code (rejected the diagnostic-endpoint approach).
- Not a replacement for unit tests; complements them.
- Not wired into per-commit CI (Tier 2 is slow/expensive; run manually/nightly).
- Multi-cluster *install* is not a Tier-2 target (SNO only, to stay tractable); multi-cluster wiring is covered at unit level already.

## 3. Settled decisions

- **Environments:** both troshkad/libvirt AND kubevirt-cluster.
- **Depth:** Tier 1 + Tier 2 e2e.
- **Form:** pytest, external black-box, `@pytest.mark.live_env`.
- **Approach A (external black-box)** chosen over in-process (B, can't exercise the real path) and diagnostic-endpoint (C, adds prod surface).
- **Precondition 1 (Tier-2 target):** Tier-2 tests are authored but **skip** when no capable target + valid OCP pull secret is configured (env-var gated, same skip mechanism as the rest — not xfail).
- **Precondition 2 (sandbox reach):** documented `oc port-forward svc/troshka-backend 8200` step in the harness README; **no new script** unless it later proves painful.

## 4. Architecture

### 4.1 Layout
```
src/backend/tests/live/
  conftest.py          # live fixtures ONLY — does NOT import tests/conftest.py (SQLite)
  config.py            # env-var driven LiveConfig + skip helpers
  helpers/
    api.py             # httpx client wrapper + poll_ocp()
    hostssh.py         # scripts/host-ssh.sh wrapper (troshkad podman)
    oc.py              # oc/kubectl wrapper (kubevirt pod) with live kubeconfig
    hostdb.py          # scripts/host-db.sh wrapper (ApiKey row lookup)
    scoped_key.py      # obtain + exercise the ops-pod:<pid> scoped key
  test_tier1_ops_pod.py        # provider-parametrized fast checks
  test_tier1_security.py       # scoped-key least-privilege + revoke
  test_tier1_bastion.py        # bastion regression (C): no ops pod
  test_tier2_install.py        # real SNO install → ready (both providers)
  test_tier2_lifecycle.py      # idempotency-restart, cancel, bastion install
  README.md            # env vars + preconditions + invocation
```

### 4.2 Marker registration & skip semantics
- Register in `src/backend/pyproject.toml` `[tool.pytest.ini_options]`:
  `markers = ["live_env: requires a running Troshka instance", "live_troshkad: requires a troshkad/libvirt host", "live_kubevirt: requires a kubevirt-cluster host", "tier2: slow real OCP install (~30-60 min)"]`
- Every live test carries `@pytest.mark.live_env`. The live `conftest.py` **skips the whole live tree** unless `TROSHKA_LIVE_URL` is set, so `pytest tests/` (unit CI) is unaffected. `testpaths` stays `["tests"]`; the live tree is collected only when selected with `-m live_env` and configured.
- Provider-specific tests additionally skip if their host isn't configured (`live_troshkad` needs `TROSHKA_LIVE_TROSHKAD_HOST`; `live_kubevirt` needs `TROSHKA_LIVE_KUBECONFIG` + `TROSHKA_LIVE_KUBEVIRT_HOST`). So the operator can run either track or both.
- `tier2` tests additionally skip unless `TROSHKA_LIVE_TIER2=1` AND the target advertises a configured pull secret (probe `GET /api/v1/auth/ocp-pull-secret`).

### 4.3 Config (env vars)
| Var | Meaning | Default |
|---|---|---|
| `TROSHKA_LIVE_URL` | base URL of the running instance | (unset ⇒ skip all) |
| `TROSHKA_LIVE_TOKEN` | `trk_…` bearer; omit to use dev-mode auto-admin | unset |
| `TROSHKA_LIVE_TROSHKAD_HOST` | host-id prefix for `host-ssh.sh` | unset ⇒ skip troshkad |
| `TROSHKA_LIVE_KUBECONFIG` | kubeconfig path for the kubevirt cluster | unset ⇒ skip kubevirt |
| `TROSHKA_LIVE_KUBEVIRT_HOST` | host-id prefix of the kubevirt-cluster host | unset ⇒ skip kubevirt |
| `TROSHKA_LIVE_TIER2` | `1` to enable real-install tests | unset ⇒ skip tier2 |
| `TROSHKA_LIVE_TIMEOUT_S` | Tier-2 install poll timeout | 4200 (70 min) |

## 5. Fixtures (`conftest.py`)

- `live_config` (session): parses env → `LiveConfig`; the module-level skip guard fires here.
- `client` (session): `httpx.Client(base_url=URL)` with `Authorization: Bearer <token>` when `TOKEN` set, else no header (dev-mode admin). Small `.get_json`/`.post_json` niceties.
- `ocp_template` (session): `GET /api/v1/projects/templates`; assert an entry with `category=="openshift"` and `id=="ocp-sno"`; return its id. Fail (not skip) if absent — a misconfigured instance should be loud.
- `project_factory` (function): `create(install_via="pod", template_id=..., **body)` → `POST /api/v1/projects/from-template` (201) → yields the project id; a finalizer **always** `DELETE /api/v1/projects/{id}` (idempotent, ignore 404) so no orphan infra even on failure/timeout. Tracks all created ids for cleanup.
- `poll_ocp(pid, until, timeout)`: polls `GET /api/v1/projects/{pid}` on `ocp_status`. Success set = `{"ready","warning"}`; failure = `"error"`; running = `"monitoring"`; not-started = `None`. Returns final status + `ocp_install_elapsed`; raises on timeout or on hitting failure when success was expected.

## 6. Assertion helpers

- `hostssh(prefix, *cmd)` → `subprocess` to `scripts/host-ssh.sh <prefix> <cmd...>`, returns stdout; used for `podman ps`/`podman inspect <container>`. Ops-pod container name = `troshka-<pid8>-ops` (matches `_ops_pod_container_name`).
- `oc(*args, kubeconfig=...)` → `subprocess` to `oc --kubeconfig=<cfg> …`; used for `get pod`, `get secret`, `describe`, `delete pod`. Namespace convention `troshka-<pid8>` (`_project_ns`).
- `hostdb(python_src)` → `subprocess` to `scripts/host-db.sh "<src>"`; the src must `from app.models.api_key import ApiKey` (not pre-imported) and print a parseable result. Used to read the `ops-pod:<pid>` row (`is_active`, `scopes`).
- `scoped_key_for(pid)` → obtains the raw `trk_…` of the `ops-pod:<pid>` key. Primary path: exec it out of the running ops pod env (`TROSHKA_API_KEY`) — troshkad via `host-ssh podman exec`, kubevirt via `oc exec`. This is the most honest (tests the pod's actual identity). Returns an `httpx.Client` bound to that key for 403/allowlist assertions.

## 7. Test catalog

### 7.1 Tier 1 — fast, no OCP install (provider-parametrized where possible)
Maps to checklist A/B/C behavioral+security items:
1. **ops pod exists & running** with the right name/namespace (troshkad `podman inspect`; kubevirt `oc get pod`).
2. **secrets not in argv** — `podman inspect` command+env contains no install-config/agent-config/pull-secret content; kubevirt: the configs are a mounted Secret (`oc get pod -o` shows a secret volume/subPath), not argv.
3. **scoped-key least-privilege** — with `scoped_key_for(pid)`: `GET /projects/{own}` → 200; `POST /projects/{own}/deploy` → 403; `DELETE /projects/{own}` → 403; `GET /projects/{other}` → 403; a websocket connect → rejected (close 4003).
4. **key active while running** — `hostdb` shows exactly one `ops-pod:<pid>` row `is_active=True`, `scopes==["topology:read","vm:exec"]`.
5. **key revoked on destroy** — after `DELETE /projects/{pid}`, the row is `is_active=False` or gone (cascade).
6. **cancel deletes the pod** — trigger cancel/undeploy → assert the ops pod/container is gone.
7. **bastion regression (C)** — an `install_via:"bastion"` project bakes a bastion VM and creates **no** ops pod (`podman ps` shows no `-ops` container / no ops pod in ns).

> **Tier-1 timing:** the ops pod is created by `_deploy_ops_pod` only *after* the cluster VMs (RHCOS) are defined+started, so Tier 1 waits for VM-up + ops-pod-created (typically minutes; the slow OCP install itself is what Tier 1 skips), asserts against that early state, then destroys. Deploy is async: `poll` until the ops pod appears with a short cap, assert, then destroy. We do NOT wait for install-complete in Tier 1; the final `DELETE`/`undeploy` cancels the in-flight install as part of teardown.

### 7.2 Tier 2 — real install (`@pytest.mark.tier2`, SNO only)
Maps to checklist A/B success items:
1. **pod SNO install → ready** on troshkad, and on kubevirt: deploy default (pod) SNO, `poll_ocp(until="ready", timeout=TIER2)`; assert terminal `ready`/`warning`, **not timeout, not error**.
2. **monitor phases advance** — `ocp_status_detail`/`ocp_install_elapsed` move forward during the run (sampled by the poller).
3. **idempotency restart** — mid-install kill the ops pod (troshkad `podman kill`/`rm`, relies on `restart_policy=always`); assert it resumes and the completed cluster is skipped (kubeconfig-exists guard) rather than reinstalled; final `ready`.
4. **bastion SNO install unchanged** — an `install_via:"bastion"` SNO reaches `ready` via the bastion path.

## 8. Safety & invocation

- **Teardown is guaranteed** by the `project_factory` finalizer (always `DELETE`), so a failed/timed-out Tier-2 run never leaks nested VMs/clusters.
- **Invocation:**
  - Tier 1 both providers: `pytest -m "live_env and not tier2" src/backend/tests/live`
  - Tier 2: `pytest -m "live_env and tier2" src/backend/tests/live` with `TROSHKA_LIVE_TIER2=1`
  - Single track: add `-m "... and live_troshkad"` or `live_kubevirt`.
- **Not in unit CI.** Optionally a `make live-env` convenience target later.
- **README** in `tests/live/` documents the env vars, the `oc port-forward svc/troshka-backend 8200` sandbox-reach step, the pull-secret precondition, and expected runtimes.

## 9. Reference facts (verified)

- Lifecycle API (all under `/api/v1`): `POST /projects/from-template` (dict body, `install_via`/`auto_install_ocp`/`template_id`/`name`/`ocp_version`/`bastion_*`/`auto_deploy`), `POST /projects/{id}/deploy` (requires `state=="draft"`), `POST /projects/{id}/undeploy` (sync destroy→draft), `DELETE /projects/{id}` (delete+cascade), `GET /projects/{id}` (`state`,`ocp_status`,`ocp_status_detail`,`ocp_install_elapsed`), `GET /projects/templates`.
- `ocp_status`: `None → "monitoring" → "ready" | "warning"` (success) or `"error"` (fail); cancellation shows via `state`, not `ocp_status`.
- Auth: `Authorization: Bearer trk_…`; dev-mode auto-admin when `oauth_enabled=false` (no header needed). Full keys minted via `POST /api/v1/api-keys`.
- Scoped ops-pod key: `ApiKey.name == f"ops-pod:{project_id}"`, `project_id` set, `scopes==["topology:read","vm:exec"]`, `is_active`; allowlist enforced in `_enforce_scoped_key_access` (only route names `get_project`/`vm_exec`, project-id matched); revoked on destroy via `_destroy_revoke_ops_pod_key`.
- `scripts/host-ssh.sh <prefix> <cmd…>` runs a remote command; `scripts/host-db.sh "<python>"` runs an inline DB query (must import `ApiKey`). No existing ops-pod/podman helper — use `host-ssh podman …`.
- SNO template id `ocp-sno` (`category: openshift`). Base URL dev default `http://localhost:8200`.

## 10. Out of scope / future

- Multi-cluster real install (Tier-2 SNO only for now).
- CI integration / AAP job wrapper (manual invocation first; revisit if it should run nightly).
- A `scripts/live-env-portforward.sh` convenience (documented manual step for now).
- NAD-reachability deep checks on kubevirt beyond "pod running + install reaches ready".
