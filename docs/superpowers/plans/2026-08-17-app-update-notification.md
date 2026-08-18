# App Update Notification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show admins a top-of-page banner when a newer Troshka version is available and let them apply it, working in image (dedicated-CI) and local-dev deployments while staying disabled where ArgoCD manages the app.

**Architecture:** A new backend `app_updater` service mirrors the existing `operator_updater` — a daemon thread caches an update-status snapshot by comparing the running pod's image digest against the `production` tag digest on quay.io. Two admin-only endpoints (`GET /update/status`, `POST /update/apply`) feed a banner in `layout.tsx`. Apply is mode-specific: in image mode it rollout-restarts the backend + frontend Deployments via an in-cluster client (new RBAC); in dev mode it spawns a detached `dev-services.sh restart backend`.

**Tech Stack:** Python 3.11/3.13, FastAPI, `kubernetes` client, `urllib`, `threading`; Next.js 15 (App Router) + PatternFly 6 frontend; pytest (SQLite + dev-auth harness).

**Spec:** `docs/superpowers/specs/2026-08-17-app-update-notification-design.md`

## Global Constraints

- **Config key is `app_update`, NOT `update`** — a Dynaconf settings object has an `.update()` method; `config.update` collides. Env overrides use `TROSHKA_APP_UPDATE__MODE`, etc.
- **Admin-only:** both endpoints use `AdminUser = Annotated[User, Depends(require_role("admin"))]`; the frontend polls/renders only when `user?.role === "admin"`.
- **Never block FastAPI startup with sync k8s calls** — `start_app_updater()` must do all Kubernetes work inside the spawned thread, not on the startup path.
- **Never show a false "update available"** — if a registry or pod digest can't be read (transient error / `None`), treat the component as up-to-date; only flag when both digests are present and differ.
- **No detached-process regression in image mode** — image apply uses the k8s rollout-restart annotation patch; only dev apply uses `subprocess.Popen(..., start_new_session=True)`.
- **Comparison tag = `production`** (configurable via `app_update.tag`), read from the running Deployment's pod spec with `production` as the default.
- **Cognitive complexity ≤ 15 per function** (SonarQube S3776) — extract helpers rather than nesting.
- **CI/dev run Python 3.13** — add extra trailing values to any `time.time()` mocks to avoid `StopIteration`.
- Run `black` (system, not venv) before every commit.

---

### Task 1: Config block + mode resolution

**Files:**
- Modify: `src/backend/config/config.yaml` (add `app_update:` block near the `auth:` block, ~line 25)
- Create: `src/backend/app/services/app_updater.py`
- Test: `src/backend/tests/test_app_updater.py`

**Interfaces:**
- Produces:
  - `resolve_mode() -> str` — returns `"dev" | "image" | "disabled"`, cached in module global `_resolved_mode`.
  - `_is_argo_managed(labels: dict) -> bool`
  - `_configured_mode() -> str`, `_oauth_enabled() -> bool`, `_read_own_deployment_labels() -> dict` — thin wrappers so tests can monkeypatch config/k8s reads.

- [ ] **Step 1: Add config block**

In `src/backend/config/config.yaml`, add (top-level, sibling of `auth:`):

```yaml
app_update:
  mode: auto          # auto | dev | image | disabled
  registry: quay.io
  repo: redhat-gpte
  tag: production
  poll_interval: 300
```

- [ ] **Step 2: Write the failing test**

Create `src/backend/tests/test_app_updater.py`:

```python
from app.services import app_updater


def _reset():
    app_updater._resolved_mode = None


def test_is_argo_managed_true():
    assert app_updater._is_argo_managed({"argocd.argoproj.io/instance": "troshka"}) is True


def test_is_argo_managed_false():
    assert app_updater._is_argo_managed({"app": "troshka-backend"}) is False


def test_resolve_mode_explicit_override(monkeypatch):
    _reset()
    monkeypatch.setattr(app_updater, "_configured_mode", lambda: "disabled")
    assert app_updater.resolve_mode() == "disabled"


def test_resolve_mode_dev_when_oauth_off(monkeypatch):
    _reset()
    monkeypatch.setattr(app_updater, "_configured_mode", lambda: "auto")
    monkeypatch.setattr(app_updater, "_oauth_enabled", lambda: False)
    assert app_updater.resolve_mode() == "dev"


def test_resolve_mode_image_when_deployed_no_argo(monkeypatch):
    _reset()
    monkeypatch.setattr(app_updater, "_configured_mode", lambda: "auto")
    monkeypatch.setattr(app_updater, "_oauth_enabled", lambda: True)
    monkeypatch.setattr(app_updater, "_read_own_deployment_labels", lambda: {})
    assert app_updater.resolve_mode() == "image"


def test_resolve_mode_disabled_when_argo(monkeypatch):
    _reset()
    monkeypatch.setattr(app_updater, "_configured_mode", lambda: "auto")
    monkeypatch.setattr(app_updater, "_oauth_enabled", lambda: True)
    monkeypatch.setattr(
        app_updater, "_read_own_deployment_labels",
        lambda: {"argocd.argoproj.io/instance": "troshka"},
    )
    assert app_updater.resolve_mode() == "disabled"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_app_updater.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.app_updater`.

- [ ] **Step 4: Write minimal implementation**

Create `src/backend/app/services/app_updater.py`:

```python
"""Detect and apply Troshka app (backend/frontend) updates.

Mirrors operator_updater but targets the app's OWN images. In image mode a
daemon thread compares the running pod digest against the production tag digest
on quay.io. Dev mode compares source mtimes against process start. Disabled
where ArgoCD manages the deployment.
"""

from __future__ import annotations

import logging

from app.core.config import config

logger = logging.getLogger(__name__)

_resolved_mode: str | None = None


def _configured_mode() -> str:
    return str(getattr(config.app_update, "mode", "auto") or "auto")


def _oauth_enabled() -> bool:
    return bool(config.auth.oauth_enabled)


def _get_own_namespace() -> str:
    import os

    ns = os.environ.get("POD_NAMESPACE")
    if ns:
        return ns
    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace") as f:
            return f.read().strip()
    except Exception:
        return "troshka"


def _read_own_deployment_labels() -> dict:
    from kubernetes import client
    from kubernetes import config as k8s_config

    k8s_config.load_incluster_config()
    apps = client.AppsV1Api()
    dep = apps.read_namespaced_deployment("troshka-backend", _get_own_namespace())
    return dep.metadata.labels or {}  # type: ignore[union-attr]


def _is_argo_managed(labels: dict) -> bool:
    return "argocd.argoproj.io/instance" in (labels or {})


def _compute_mode() -> str:
    configured = _configured_mode()
    if configured != "auto":
        return configured
    if not _oauth_enabled():
        return "dev"
    try:
        labels = _read_own_deployment_labels()
    except Exception:
        labels = {}
    return "disabled" if _is_argo_managed(labels) else "image"


def resolve_mode() -> str:
    global _resolved_mode
    if _resolved_mode is None:
        _resolved_mode = _compute_mode()
    return _resolved_mode
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_app_updater.py -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/app_updater.py src/backend/tests/test_app_updater.py
git add src/backend/config/config.yaml src/backend/app/services/app_updater.py src/backend/tests/test_app_updater.py
git commit -m "feat(update): app_updater config + mode resolution"
```

---

### Task 2: Image-mode status snapshot + poller

**Files:**
- Modify: `src/backend/app/services/app_updater.py`
- Test: `src/backend/tests/test_app_updater.py`

**Interfaces:**
- Consumes: `resolve_mode()` (Task 1); `_extract_digest_from_pod(pod)` imported from `app.services.operator_updater`.
- Produces:
  - `_fetch_registry_digest(image: str, tag: str) -> str | None`
  - `_read_own_digests() -> dict` — `{"backend": digest|None, "frontend": digest|None}`
  - `_read_rolling_out() -> bool`
  - `_build_image_snapshot() -> dict` — `{"up_to_date": bool, "rolling_out": bool, "components": {"backend": {"current","available"}, "frontend": {...}}}`
  - `start_app_updater() -> threading.Thread` (poller); module global `_snapshot: dict`.

- [ ] **Step 1: Write the failing test**

Append to `src/backend/tests/test_app_updater.py`:

```python
def test_build_image_snapshot_up_to_date(monkeypatch):
    monkeypatch.setattr(
        app_updater, "_read_own_digests",
        lambda: {"backend": "sha256:aaa", "frontend": "sha256:bbb"},
    )
    monkeypatch.setattr(app_updater, "_read_rolling_out", lambda: False)
    digests = {"troshka-backend": "sha256:aaa", "troshka-frontend": "sha256:bbb"}
    monkeypatch.setattr(
        app_updater, "_fetch_registry_digest",
        lambda image, tag: digests[image.rsplit("/", 1)[1]],
    )
    snap = app_updater._build_image_snapshot()
    assert snap["up_to_date"] is True
    assert snap["components"]["backend"]["current"] == "sha256:aaa"


def test_build_image_snapshot_backend_outdated(monkeypatch):
    monkeypatch.setattr(
        app_updater, "_read_own_digests",
        lambda: {"backend": "sha256:old", "frontend": "sha256:bbb"},
    )
    monkeypatch.setattr(app_updater, "_read_rolling_out", lambda: False)
    digests = {"troshka-backend": "sha256:NEW", "troshka-frontend": "sha256:bbb"}
    monkeypatch.setattr(
        app_updater, "_fetch_registry_digest",
        lambda image, tag: digests[image.rsplit("/", 1)[1]],
    )
    snap = app_updater._build_image_snapshot()
    assert snap["up_to_date"] is False


def test_build_image_snapshot_missing_digest_not_flagged(monkeypatch):
    # transient failure -> registry digest None -> must NOT flag an update
    monkeypatch.setattr(
        app_updater, "_read_own_digests",
        lambda: {"backend": "sha256:aaa", "frontend": "sha256:bbb"},
    )
    monkeypatch.setattr(app_updater, "_read_rolling_out", lambda: False)
    monkeypatch.setattr(app_updater, "_fetch_registry_digest", lambda image, tag: None)
    snap = app_updater._build_image_snapshot()
    assert snap["up_to_date"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_app_updater.py -k image_snapshot -v`
Expected: FAIL — `AttributeError: ... _build_image_snapshot`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/backend/app/services/app_updater.py` (imports at top: add `import json`, `import threading`, `import time`, `import urllib.request`):

```python
COMPONENTS = {
    "backend": "troshka-backend",
    "frontend": "troshka-frontend",
}

_snapshot: dict = {}


def _registry() -> str:
    return str(getattr(config.app_update, "registry", "quay.io") or "quay.io")


def _repo() -> str:
    return str(getattr(config.app_update, "repo", "redhat-gpte") or "redhat-gpte")


def _tag() -> str:
    return str(getattr(config.app_update, "tag", "production") or "production")


def _poll_interval() -> int:
    return int(getattr(config.app_update, "poll_interval", 300) or 300)


def _fetch_registry_digest(image: str, tag: str) -> str | None:
    url = f"https://{_registry()}/v2/{image}/manifests/{tag}"
    req = urllib.request.Request(
        url, headers={"Accept": "application/vnd.oci.image.manifest.v1+json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            digest = resp.headers.get("Docker-Content-Digest")
            if digest:
                return digest
            body = json.loads(resp.read())
            return body.get("config", {}).get("digest")
    except Exception as e:
        logger.warning("Failed to fetch %s:%s digest from registry: %s", image, tag, e)
        return None


def _read_own_digests() -> dict:
    from kubernetes import client
    from kubernetes import config as k8s_config

    from app.services.operator_updater import _extract_digest_from_pod

    k8s_config.load_incluster_config()
    core = client.CoreV1Api()
    ns = _get_own_namespace()
    out: dict = {}
    for name in COMPONENTS:
        out[name] = None
        try:
            pods = core.list_namespaced_pod(
                namespace=ns, label_selector=f"app=troshka-{name}"
            )
            for pod in pods.items or []:  # type: ignore[union-attr]
                digest = _extract_digest_from_pod(pod)
                if digest:
                    out[name] = digest
                    break
        except Exception:
            logger.debug("Failed to read %s pod digest", name, exc_info=True)
    return out


def _read_rolling_out() -> bool:
    from kubernetes import client
    from kubernetes import config as k8s_config

    k8s_config.load_incluster_config()
    apps = client.AppsV1Api()
    ns = _get_own_namespace()
    for suffix in COMPONENTS.values():
        try:
            dep = apps.read_namespaced_deployment(name=suffix, namespace=ns)
            desired = dep.spec.replicas or 1  # type: ignore[union-attr]
            updated = dep.status.updated_replicas or 0  # type: ignore[union-attr]
            ready = dep.status.ready_replicas or 0  # type: ignore[union-attr]
            if updated < desired or ready < desired:
                return True
        except Exception:
            logger.debug("Failed to read %s rollout status", suffix, exc_info=True)
    return False


def _build_image_snapshot() -> dict:
    running = _read_own_digests()
    rolling = _read_rolling_out()
    comps: dict = {}
    up_to_date = True
    for name, suffix in COMPONENTS.items():
        available = _fetch_registry_digest(f"{_repo()}/{suffix}", _tag())
        current = running.get(name)
        comps[name] = {"current": current, "available": available}
        if current and available and current != available:
            up_to_date = False
    return {"up_to_date": up_to_date, "rolling_out": rolling, "components": comps}


def _poll() -> None:
    global _snapshot
    try:
        _snapshot = _build_image_snapshot()
    except Exception:
        logger.exception("app update poll failed")


def _poller_loop() -> None:
    time.sleep(10)
    mode = resolve_mode()
    if mode != "image":
        logger.info("app_updater: mode=%s, polling disabled", mode)
        return
    _poll()
    while True:
        time.sleep(_poll_interval())
        try:
            _poll()
        except Exception:
            logger.exception("app update poll failed")


def start_app_updater() -> threading.Thread:
    """Start the background updater. All k8s work happens inside the thread."""
    thread = threading.Thread(target=_poller_loop, daemon=True, name="app-updater")
    thread.start()
    return thread
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_app_updater.py -v`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/app_updater.py src/backend/tests/test_app_updater.py
git add src/backend/app/services/app_updater.py src/backend/tests/test_app_updater.py
git commit -m "feat(update): image-mode digest snapshot + poller"
```

---

### Task 3: Dev-mode status + unified get_status/apply_update

**Files:**
- Modify: `src/backend/app/services/app_updater.py`
- Test: `src/backend/tests/test_app_updater.py`

**Interfaces:**
- Consumes: `resolve_mode()`, `_snapshot` (Tasks 1-2).
- Produces:
  - `_dev_up_to_date() -> bool`
  - `get_status() -> dict` — `{"mode": ...}` plus `up_to_date`/`rolling_out`/`components` for dev & image; `{"mode": "disabled"}` otherwise.
  - `apply_update() -> dict` — dispatches to `_apply_image()` / `_apply_dev()`.
  - `_apply_image() -> dict` → `{"status": "rolling_out"}`
  - `_apply_dev() -> dict` → `{"status": "restarting"}`

- [ ] **Step 1: Write the failing test**

Append to `src/backend/tests/test_app_updater.py`:

```python
def test_get_status_disabled(monkeypatch):
    _reset()
    monkeypatch.setattr(app_updater, "resolve_mode", lambda: "disabled")
    assert app_updater.get_status() == {"mode": "disabled"}


def test_get_status_dev(monkeypatch):
    _reset()
    monkeypatch.setattr(app_updater, "resolve_mode", lambda: "dev")
    monkeypatch.setattr(app_updater, "_dev_up_to_date", lambda: False)
    status = app_updater.get_status()
    assert status["mode"] == "dev"
    assert status["up_to_date"] is False
    assert status["rolling_out"] is False


def test_get_status_image(monkeypatch):
    _reset()
    monkeypatch.setattr(app_updater, "resolve_mode", lambda: "image")
    app_updater._snapshot = {"up_to_date": False, "rolling_out": False, "components": {}}
    status = app_updater.get_status()
    assert status["mode"] == "image"
    assert status["up_to_date"] is False


def test_apply_update_dev_spawns_restart(monkeypatch):
    _reset()
    monkeypatch.setattr(app_updater, "resolve_mode", lambda: "dev")
    calls = {}

    class FakePopen:
        def __init__(self, args, cwd=None, start_new_session=False):
            calls["args"] = args
            calls["cwd"] = cwd
            calls["detached"] = start_new_session

    monkeypatch.setattr(app_updater.subprocess, "Popen", FakePopen)
    result = app_updater.apply_update()
    assert result == {"status": "restarting"}
    assert calls["args"] == ["./dev-services.sh", "restart", "backend"]
    assert calls["detached"] is True


def test_apply_update_image_patches_both_deployments(monkeypatch):
    _reset()
    monkeypatch.setattr(app_updater, "resolve_mode", lambda: "image")
    patched = []
    monkeypatch.setattr(app_updater, "_patch_restart", lambda name: patched.append(name))
    result = app_updater.apply_update()
    assert result == {"status": "rolling_out"}
    assert set(patched) == {"troshka-backend", "troshka-frontend"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_app_updater.py -k "status or apply" -v`
Expected: FAIL — `get_status`/`apply_update` not defined.

- [ ] **Step 3: Write minimal implementation**

Add to `src/backend/app/services/app_updater.py` (add `import datetime`, `import os`, `import subprocess` and `from pathlib import Path` at top):

```python
_PROCESS_START = time.time()


def _backend_src_dir() -> Path:
    return Path(__file__).resolve().parents[2]  # src/backend


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]  # repo root


def _dev_up_to_date() -> bool:
    newest = 0.0
    for path in _backend_src_dir().rglob("*.py"):
        try:
            mtime = path.stat().st_mtime
            if mtime > newest:
                newest = mtime
        except OSError:
            continue
    return newest <= _PROCESS_START


def get_status() -> dict:
    mode = resolve_mode()
    if mode == "disabled":
        return {"mode": "disabled"}
    if mode == "dev":
        return {
            "mode": "dev",
            "up_to_date": _dev_up_to_date(),
            "rolling_out": False,
            "components": {},
        }
    snap = _snapshot or {"up_to_date": True, "rolling_out": False, "components": {}}
    return {"mode": "image", **snap}


def _patch_restart(name: str) -> None:
    from kubernetes import client
    from kubernetes import config as k8s_config

    k8s_config.load_incluster_config()
    apps = client.AppsV1Api()
    ts = datetime.datetime.now(datetime.UTC).isoformat()
    apps.patch_namespaced_deployment(
        name=name,
        namespace=_get_own_namespace(),
        body={
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {"kubectl.kubernetes.io/restartedAt": ts}
                    }
                }
            }
        },
    )


def _apply_image() -> dict:
    for suffix in COMPONENTS.values():
        _patch_restart(suffix)
    return {"status": "rolling_out"}


def _apply_dev() -> dict:
    subprocess.Popen(
        ["./dev-services.sh", "restart", "backend"],
        cwd=str(_repo_root()),
        start_new_session=True,
    )
    return {"status": "restarting"}


def apply_update() -> dict:
    mode = resolve_mode()
    if mode == "image":
        return _apply_image()
    if mode == "dev":
        return _apply_dev()
    raise ValueError(f"apply_update not supported in mode={mode}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_app_updater.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/app_updater.py src/backend/tests/test_app_updater.py
git add src/backend/app/services/app_updater.py src/backend/tests/test_app_updater.py
git commit -m "feat(update): dev-mode status + unified get_status/apply_update"
```

---

### Task 4: Endpoints + startup wiring

**Files:**
- Create: `src/backend/app/api/updates.py`
- Modify: `src/backend/app/main.py` (register router near other `include_router` calls; call `start_app_updater()` in the startup block at ~line 470, right after `start_operator_updater()`)
- Test: `src/backend/tests/test_app_updater.py`

**Interfaces:**
- Consumes: `app_updater.get_status()`, `app_updater.apply_update()`, `app_updater.resolve_mode()`; `require_role` from `app.core.auth`.
- Produces: `GET /api/v1/update/status`, `POST /api/v1/update/apply`.

- [ ] **Step 1: Write the failing test**

Append to `src/backend/tests/test_app_updater.py` (uses the project's existing TestClient fixture — mirror how other `tests/test_*.py` build the client; if a shared `client` fixture exists in `conftest.py`, use it):

```python
from fastapi.testclient import TestClient

from app.main import app


def test_status_endpoint_returns_snapshot(monkeypatch):
    monkeypatch.setattr(
        "app.services.app_updater.get_status",
        lambda: {"mode": "dev", "up_to_date": False, "rolling_out": False, "components": {}},
    )
    with TestClient(app) as client:
        resp = client.get("/api/v1/update/status")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "dev"


def test_apply_endpoint_dispatches(monkeypatch):
    monkeypatch.setattr("app.services.app_updater.resolve_mode", lambda: "dev")
    monkeypatch.setattr(
        "app.services.app_updater.apply_update", lambda: {"status": "restarting"}
    )
    with TestClient(app) as client:
        resp = client.post("/api/v1/update/apply")
    assert resp.status_code == 200
    assert resp.json()["status"] == "restarting"


def test_apply_endpoint_disabled_returns_400(monkeypatch):
    monkeypatch.setattr("app.services.app_updater.resolve_mode", lambda: "disabled")
    with TestClient(app) as client:
        resp = client.post("/api/v1/update/apply")
    assert resp.status_code == 400
```

> Note: tests run in dev auth mode (auto-admin), so `require_role("admin")` passes. If the suite starts a background poller on app startup, that's fine — `start_app_updater()` resolves `dev` mode (oauth off) and the thread exits without k8s calls.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_app_updater.py -k endpoint -v`
Expected: FAIL — 404 (routes not registered).

- [ ] **Step 3: Write minimal implementation**

Create `src/backend/app/api/updates.py`:

```python
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import require_role
from app.models.user import User
from app.services import app_updater

router = APIRouter(prefix="/update", tags=["update"])

AdminUser = Annotated[User, Depends(require_role("admin"))]


@router.get("/status")
def get_update_status(user: AdminUser):
    return app_updater.get_status()


@router.post("/apply")
def apply_update(user: AdminUser):
    if app_updater.resolve_mode() == "disabled":
        raise HTTPException(
            status_code=400, detail="Updates are managed externally (ArgoCD)"
        )
    return app_updater.apply_update()
```

In `src/backend/app/main.py`:
- Add `from app.api import updates` with the other api imports, and register it the same way the other routers are registered (they include the `/api/v1` prefix — match the existing `app.include_router(...)` calls, e.g. `app.include_router(updates.router, prefix=_API_PREFIX)`).
- In the startup block, immediately after `start_operator_updater()` (~line 470):

```python
    from app.services.app_updater import start_app_updater

    start_app_updater()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_app_updater.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/api/updates.py src/backend/app/main.py src/backend/tests/test_app_updater.py
git add src/backend/app/api/updates.py src/backend/app/main.py src/backend/tests/test_app_updater.py
git commit -m "feat(update): status + apply endpoints, startup wiring"
```

---

### Task 5: RBAC + POD_NAMESPACE for self rollout-restart (image mode)

**Files:**
- Create: `deploy/base/backend-update-rbac.yaml` (Role + RoleBinding)
- Modify: `deploy/base/kustomization.yaml` (add the new file to `resources`)
- Modify: `deploy/base/backend-deployment.yaml` (add `POD_NAMESPACE` downward-API env)
- Modify: `deploy/helm/templates/` (add the equivalent Role/RoleBinding + env; follow the chart's existing template conventions and `values.yaml` namespace/SA references)

**Interfaces:**
- Consumes: the backend Deployment's `serviceAccountName` and namespace.
- Produces: RBAC allowing the backend pod's SA to read + patch the backend/frontend Deployments in its own namespace, and a `POD_NAMESPACE` env var for `_get_own_namespace()`.

- [ ] **Step 1: Find the backend ServiceAccount + namespace**

Run: `grep -n "serviceAccountName\|namespace" /Users/prutledg/troshka/deploy/base/backend-deployment.yaml /Users/prutledg/troshka/deploy/base/kustomization.yaml`
Note the SA name (if none is set, the pod uses `default`) and the base namespace. Use those exact values in Step 2.

- [ ] **Step 2: Write the RBAC manifest**

Create `deploy/base/backend-update-rbac.yaml` (replace `<SA_NAME>` and `<NAMESPACE>` with the values from Step 1):

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: troshka-self-update
  namespace: <NAMESPACE>
rules:
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "watch", "patch"]
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: troshka-self-update
  namespace: <NAMESPACE>
subjects:
  - kind: ServiceAccount
    name: <SA_NAME>
    namespace: <NAMESPACE>
roleRef:
  kind: Role
  name: troshka-self-update
  apiGroup: rbac.authorization.k8s.io
```

- [ ] **Step 3: Add POD_NAMESPACE downward-API env**

In `deploy/base/backend-deployment.yaml`, add to the backend container's `env:` list:

```yaml
            - name: POD_NAMESPACE
              valueFrom:
                fieldRef:
                  fieldPath: metadata.namespace
```

- [ ] **Step 4: Register in kustomization**

Add `- backend-update-rbac.yaml` to the `resources:` list in `deploy/base/kustomization.yaml`.

- [ ] **Step 5: Mirror in the Helm chart**

Add the same Role/RoleBinding as a template under `deploy/helm/templates/` (e.g. `backend-update-rbac.yaml`, using the chart's namespace/SA values) and the `POD_NAMESPACE` env in the Helm backend deployment template. Follow existing template style (`{{ .Release.Namespace }}` / the chart's SA value).

- [ ] **Step 6: Verify manifests render**

Run: `kustomize build /Users/prutledg/troshka/deploy/base >/dev/null && echo BASE_OK`
Run: `helm template /Users/prutledg/troshka/deploy/helm >/dev/null && echo HELM_OK`
Expected: `BASE_OK` and `HELM_OK` (no YAML/template errors). The Role/RoleBinding and `POD_NAMESPACE` env appear in the rendered output (`kustomize build ... | grep -A2 POD_NAMESPACE`).

- [ ] **Step 7: Commit**

```bash
cd /Users/prutledg/troshka
git add deploy/base/backend-update-rbac.yaml deploy/base/kustomization.yaml deploy/base/backend-deployment.yaml deploy/helm/
git commit -m "feat(update): RBAC + POD_NAMESPACE for self rollout-restart"
```

---

### Task 6: Frontend update banner

**Files:**
- Modify: `src/frontend/src/app/layout.tsx`

**Interfaces:**
- Consumes: `GET /api/v1/update/status`, `POST /api/v1/update/apply`; existing `isAdmin`, `backendDown`.
- Produces: a dismissible info banner rendered directly after the `backendDown` div.

> Heed `src/frontend/AGENTS.md`: this is PatternFly 6 + a modified Next.js — follow the existing patterns already in `layout.tsx` (raw `fetch`, `useState`/`useEffect`, the `navWarnings` poll and `backendDown` banner) rather than importing unfamiliar APIs. Verify `Button` is already imported at the top of `layout.tsx`; if not, add it to the existing `@patternfly/react-core` import.

- [ ] **Step 1: Add state + polling effect**

After the `navWarnings` effect (~line 166) add:

```tsx
  const [updateStatus, setUpdateStatus] = useState<any>(null);
  const [applying, setApplying] = useState(false);
  const [dismissedKey, setDismissedKey] = useState<string | null>(null);
  useEffect(() => { setDismissedKey(localStorage.getItem("troshka-update-dismissed")); }, []);
  useEffect(() => {
    if (!isAdmin) return;
    const check = () => {
      fetch("/api/v1/update/status")
        .then((r) => (r.ok ? r.json() : null))
        .then(setUpdateStatus)
        .catch(() => {});
    };
    check();
    const iv = setInterval(check, 60000);
    return () => clearInterval(iv);
  }, [isAdmin]);

  const updateTargetKey =
    updateStatus?.mode === "image"
      ? JSON.stringify(updateStatus?.components || {})
      : "dev";
  const showUpdate =
    isAdmin &&
    updateStatus &&
    updateStatus.mode !== "disabled" &&
    updateStatus.up_to_date === false &&
    dismissedKey !== updateTargetKey;
  const updateBusy = applying || updateStatus?.rolling_out;

  const applyUpdate = () => {
    setApplying(true);
    fetch("/api/v1/update/apply", { method: "POST" }).catch(() => {});
    // leave applying=true; the backendDown banner covers the restart gap and
    // the status poll clears this banner once the new version is up.
  };
  const dismissUpdate = () => {
    localStorage.setItem("troshka-update-dismissed", updateTargetKey);
    setDismissedKey(updateTargetKey);
  };
```

- [ ] **Step 2: Render the banner**

Immediately after the `backendDown` block (after line 352, before the `{!authChecked ...}` block) add:

```tsx
          {showUpdate && (
            <div style={{
              background: "rgba(59, 130, 246, 0.15)",
              border: "1px solid rgba(59, 130, 246, 0.4)",
              color: "#93c5fd",
              padding: "8px 16px",
              fontSize: 13,
              textAlign: "center",
              fontWeight: 500,
              display: "flex",
              gap: 12,
              justifyContent: "center",
              alignItems: "center",
            }}>
              <span>A newer version of Troshka is available.</span>
              <Button variant="primary" isDisabled={updateBusy} isLoading={updateBusy} onClick={applyUpdate}>
                {updateBusy
                  ? "Update in progress…"
                  : updateStatus?.mode === "dev"
                    ? "Restart backend"
                    : "Apply update"}
              </Button>
              <Button variant="link" isInline onClick={dismissUpdate}>Dismiss</Button>
            </div>
          )}
```

- [ ] **Step 3: Type-check the frontend**

Run: `cd /Users/prutledg/troshka/src/frontend && npx tsc --noEmit`
Expected: no new errors from `layout.tsx` (if `Button` props like `isInline`/`isLoading` differ in this PF6 build, adjust to the props the compiler accepts — confirm against the other `Button` usages already in `layout.tsx`).

- [ ] **Step 4: Manual verification (dev mode)**

With `./dev-services.sh start` running and logged in as admin: touch a backend file (`touch src/backend/app/main.py`), wait for the 60s poll (or reload), confirm the blue banner with **"Restart backend"** appears. Click it; confirm the backend restarts (the red `backendDown` banner flashes) and the update banner clears once it's back. Confirm **Dismiss** hides it and it stays hidden until a newer change.

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka
git add src/frontend/src/app/layout.tsx
git commit -m "feat(update): admin update-available banner in layout"
```

---

### Task 7: Docs

**Files:**
- Modify: `docs/dev/deployment.md` (or `docs/dev/subsystems.md`) — document the update-notification subsystem.

- [ ] **Step 1: Document the feature**

Add a short section: the three modes (`dev`/`image`/`disabled`), the `app_update` config block + env overrides, the `production`-digest comparison, the self rollout-restart RBAC requirement, and that ArgoCD-managed deployments auto-detect as `disabled`. Note the two endpoints and the admin-only banner.

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka
git add docs/dev/deployment.md
git commit -m "docs(update): document app update-notification subsystem"
```

---

## Self-Review

**Spec coverage:**
- Mode detection & config (spec §1) → Task 1.
- Backend service + poller, image detection (spec §2, §3 detect) → Tasks 2, 4.
- Dev-mode detection (spec §2) → Task 3.
- Status + apply endpoints, admin gating (spec §2) → Tasks 3, 4.
- Image apply + RBAC + POD_NAMESPACE (spec §3) → Tasks 3, 5.
- Dev apply / detached restart (spec §4) → Task 3.
- Frontend banner (spec §5) → Task 6.
- Error handling — no false positives / 403 gating / 400 disabled (spec §6) → Tasks 2 (missing-digest test), 4 (400 test), endpoint `AdminUser`.
- Testing (spec §7) → tests in Tasks 1-4; manual for deploy/frontend (Tasks 5, 6).

**Placeholder scan:** `<SA_NAME>`/`<NAMESPACE>` in Task 5 are resolved by the Step 1 grep before use (deliberate, not a TODO). No other placeholders.

**Type consistency:** `resolve_mode`, `get_status`, `apply_update`, `_build_image_snapshot`, `_patch_restart`, `_read_own_digests`, `_fetch_registry_digest(image, tag)`, `_dev_up_to_date` are used with the same signatures across tasks and tests. Config key is `app_update` everywhere. Digest keys `current`/`available` and component keys `backend`/`frontend` are consistent between backend snapshot and frontend banner.
