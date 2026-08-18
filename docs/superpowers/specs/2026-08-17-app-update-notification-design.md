# App Update Notification — Design

**Date:** 2026-08-17
**Status:** Approved (design), pending implementation plan
**Author:** brainstormed with Patrick Rutledge

## Problem

Troshka has no way to tell an operator that a newer version of the app
(backend/frontend) is available, nor to apply it. Today the app version is a
hardcoded `"0.1.0"` in three places and there is no self-version tracking.

We want an admin-visible banner at the top of the UI that says an update is
available and offers to apply it, working across two deployment realities:

- **Image-based, no ArgoCD** (dedicated-CI deployments) — the primary target.
- **Local dev** (`dev-services.sh`).

Where **ArgoCD** manages the deployment (infra01 prod), Argo owns updates and
this feature stays out of the way entirely.

## Non-goals (YAGNI)

- No semver / git-SHA / changelog display. Digest identity is sufficient for the
  admin/CI audience. (Git-SHA stamping is a noted future nicety, not in scope.)
- No per-component apply — backend and frontend restart together.
- No passive "up to date / Argo manages this" indicator in Argo mode. The
  feature is simply off there.
- No automatic apply. Detection is automatic; applying is always an explicit
  admin action.

## Design overview

Mirror the existing `operator_updater` machinery, pointed at the app's *own*
backend/frontend images instead of the remote provider operator:

- A new `app_updater` background service caches an update-status snapshot.
- `GET /api/v1/update/status` feeds the banner.
- `POST /api/v1/update/apply` performs the mode-specific apply action.

The rejected alternative — generalizing `operator_updater` itself to accept a
target — was declined because `operator_updater` builds Kubernetes clients from
*remote provider credentials* for a different cluster, whereas app self-update is
**in-cluster** via the pod's own ServiceAccount. Folding them together would
tangle two distinct concerns.

### Existing code being reused / mirrored

- `src/backend/app/services/operator_updater.py`
  - `_fetch_registry_digest(...)` — quay.io digest for a tag (generalize to take
    image + tag).
  - `_extract_digest_from_pod(...)` / `_read_pod_digest(...)` — running pod
    image digest by label selector.
  - `_read_deployment_info(...)` — read image/tag from a Deployment pod spec.
  - `update_operator()` rollout-restart pattern — patch
    `template.metadata.annotations` with a `restartedAt` timestamp.
  - `start_operator_updater()` daemon-thread + `main.py` startup wiring
    (`src/backend/app/main.py:468-470`).
- In-cluster client precedent: `_get_k8s_client()` /
  `k8s_config.load_incluster_config()` in `src/backend/app/core/auth.py:22-38`.
- Admin gating: `AdminUser = Annotated[User, Depends(require_role("admin"))]`
  (`src/backend/app/api/hosts.py:30-31`, `require_role` in
  `src/backend/app/core/auth.py:322-332`).
- Frontend banner slot + polling patterns: the `backendDown` styled div and
  `navWarnings` poll in `src/frontend/src/app/layout.tsx`.

## 1. Mode detection & config

New `update` config block in `src/backend/config/config.yaml`, overridable via
Dynaconf env vars (`TROSHKA_UPDATE__MODE`, etc.):

```yaml
update:
  mode: auto          # auto | dev | image | disabled
  registry: quay.io
  repo: redhat-gpte
  tag: production
  poll_interval: 300
```

`mode: auto` resolves at startup to:

1. `not config.auth.oauth_enabled` → **dev** (the canonical dev-mode signal,
   already surfaced by `GET /api/v1/auth/config` as `dev_mode`).
2. otherwise, read the backend's own Deployment; if it carries an
   `argocd.argoproj.io/instance` label → **disabled** ("let Argo update it").
3. otherwise → **image**.

An explicit `mode` value (`dev`/`image`/`disabled`) overrides auto-detection.

The comparison tag is `production` per requirement: an update is available in
image mode only when the `production` registry digest differs from the running
(local) pod digest. The tag is read from the running Deployment's pod spec,
defaulting to `production`.

## 2. Backend service & endpoints

### `src/backend/app/services/app_updater.py`

- `start_app_updater()` — a `threading.Thread(daemon=True)` started from
  `main.py` startup next to `start_operator_updater()`. It only actively polls in
  **image** mode.
- **Image mode:** every `poll_interval` seconds, for both `troshka-backend` and
  `troshka-frontend`:
  - running pod digest — label selector `app=troshka-backend` /
    `app=troshka-frontend`, in the backend's own namespace.
  - registry digest for the resolved tag.
  - `up_to_date = backend_matches and frontend_matches`.
  - track `rolling_out` (a rollout in progress, mirroring operator-status
    semantics).
  Cache the snapshot in module state (Redis not required; matches
  `operator_updater`).
- **Dev mode:** no polling thread work; status is computed on demand in the
  endpoint — newest mtime of files under `src/backend` vs the backend process
  start time. `up_to_date = newest_mtime <= process_start_time`.
- **Own-namespace discovery:** read `POD_NAMESPACE` (added via downward API on
  the Deployment), falling back to
  `/var/run/secrets/kubernetes.io/serviceaccount/namespace`.

### `src/backend/app/api/updates.py` (router prefix `/update`, registered in `main.py`)

- `GET /api/v1/update/status` — `AdminUser`. Returns:
  ```json
  {
    "mode": "image|dev|disabled",
    "up_to_date": true,
    "rolling_out": false,
    "components": {
      "backend":  {"current": "sha256:...", "available": "sha256:..."},
      "frontend": {"current": "sha256:...", "available": "sha256:..."}
    }
  }
  ```
  `disabled` mode returns `{"mode": "disabled"}` and the UI renders nothing.
- `POST /api/v1/update/apply` — `AdminUser`. Mode-specific (sections 3 & 4).
  Returns `400` in `disabled` mode.

## 3. Image apply + RBAC

`apply` in image mode:

- Build an in-cluster client (`load_incluster_config()`).
- Patch the `kubectl.kubernetes.io/restartedAt` annotation on **both**
  `troshka-backend` and `troshka-frontend` Deployments (own namespace) with a
  current timestamp.
- The `production` repull picks up the new digest; the Service routes to the new
  pods as they become ready.
- Return `{"status": "rolling_out"}`.

New RBAC (in `deploy/base` and the Helm chart):

- A `Role` in the `troshka` namespace: `get`/`list`/`watch`/`patch` on
  `deployments` (apps), `get`/`list` on `pods`.
- A `RoleBinding` binding that Role to the backend Deployment's ServiceAccount.
- Add a `POD_NAMESPACE` downward-API env var to the backend Deployment.

## 4. Dev apply

`apply` in dev mode:

- Spawn a **detached** child from the repo root:
  `subprocess.Popen(["./dev-services.sh", "restart", "backend"], start_new_session=True)`.
- Return `202 {"status": "restarting"}` *before* uvicorn is killed, so the client
  gets a response.
- `dev-services.sh restart backend` already guards on in-flight deploy work via
  `GET /api/v1/debug/threads` (`check_backend_idle`).
- Frontend hot-reloads independently; only the backend restarts.

This detached `Popen(start_new_session=True)` is the one genuinely new pattern —
all existing backend subprocess use is blocking `subprocess.run`.

## 5. Frontend banner (`src/frontend/src/app/layout.tsx`)

- New `updateStatus` state, polled from `GET /api/v1/update/status` every 60s,
  **admin-only** (`user?.role === "admin"`, same guard as `navWarnings`).
- When `up_to_date === false`, render a **dismissible info banner** in the
  existing `backendDown` slot (first child of `<Page>`, above `{children}`):
  - Text: "A newer version of Troshka is available."
  - Button label: **"Apply update"** (image) or **"Restart backend"** (dev).
  - While `rolling_out` / after clicking: show "Update in progress…" and disable
    the button.
- Dismissal persists in `localStorage`, keyed by the target digest (image) or
  process-change marker (dev), so a dismissed banner reappears when a *newer*
  update lands.
- On click → `POST /api/v1/update/apply`. The existing `backendDown` banner
  covers the reconnect gap during restart; once the backend is back, the status
  poll clears the update banner.

## 6. Error handling

- Registry/pod digest fetch failures: log and leave the last known snapshot
  (or `up_to_date: true` / unknown) — never show a false "update available"
  on a transient network error. Mirror `operator_updater` resilience.
- `apply` in image mode with missing RBAC: surface the Kubernetes `403` as a
  clear error to the admin (banner shows failure, does not silently swallow).
- `apply` in dev mode when the repo root / `dev-services.sh` is not found:
  return an error rather than a false success.
- Non-admin callers: `403` from `require_role("admin")` (endpoint-level), and the
  banner is never rendered/polled for non-admins.

## 7. Testing

Backend unit tests (SQLite + dev-auth harness, per project conventions):

- Mode resolution: dev (`oauth_enabled=false`), image (in-cluster, no Argo
  label), disabled (Argo label present), and explicit-override cases.
- `up_to_date` logic: image (backend/frontend digest match/mismatch matrix) and
  dev (mtime before/after process start).
- Apply dispatch: image path asserts the k8s patch is called for **both**
  deployments; dev path asserts a detached `Popen` for `dev-services.sh restart
  backend` (mocked).
- Admin gating: `403` for non-admin on both endpoints.

No frontend test framework exists in the repo (raw `fetch`, no test harness), so
the banner is verified manually across dev and a dedicated-CI image deployment.

## Files touched (anticipated)

- `src/backend/config/config.yaml` — new `update` block.
- `src/backend/app/services/app_updater.py` — new service.
- `src/backend/app/api/updates.py` — new router; register in
  `src/backend/app/main.py`.
- `src/backend/app/main.py` — start `app_updater`, register router,
  (optionally) capture process start time.
- `deploy/base/*` and `deploy/helm/*` — RBAC (Role/RoleBinding), `POD_NAMESPACE`
  downward-API env.
- `src/frontend/src/app/layout.tsx` — update-status poll + banner.
- `src/backend/tests/` — new tests for `app_updater` and the endpoints.
