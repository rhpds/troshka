# Live-Environment Verification Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automate the Plan 4b [LIVE-ENV] checklist as a pytest harness that drives a REAL running Troshka instance and verifies the OCP pod-install path on both troshkad/libvirt and kubevirt-cluster hosts (Tier 1 fast + Tier 2 real install).

**Architecture:** External black-box pytest suite under `src/backend/tests/live/`. It talks to a live instance over HTTP and asserts host-side truth by shelling to `scripts/host-ssh.sh` (troshkad `podman`), `oc` (kubevirt), and `scripts/host-db.sh` (the `ops-pod:<pid>` key row). Adds NO production/API code. The live tests skip unless env-configured, so unit CI is untouched; the reusable helper layer is unit-tested with mocked subprocess/httpx and runs in normal CI.

**Tech Stack:** Python 3.11 (CI 3.13), pytest, httpx (already a FastAPI/starlette dependency), `subprocess`; existing `scripts/host-ssh.sh` / `scripts/host-db.sh`; `oc`.

**Spec:** `docs/superpowers/specs/2026-09-02-live-env-verification-harness-design.md`

## Global Constraints

- **No production code changes.** Only new files under `src/backend/tests/live/`, one `pyproject.toml` marker registration, and doc updates. If a task needs a backend change, STOP and report.
- **Unit CI unaffected:** `pytest src/backend/tests` (no live env vars) must run all existing tests + the new *helper unit tests*, and must SKIP every `live_env`-marked test. Never let a live test run without configuration.
- **Reuse, don't reinvent:** host assertions go through `scripts/host-ssh.sh <prefix> <cmd…>`, `scripts/host-db.sh "<python>"`, and `oc --kubeconfig=<cfg> …`. No new production endpoints.
- **Guaranteed teardown:** every project created by a test is `DELETE`d in a finalizer even on failure/timeout — no orphan infra.
- **Tier 2 = SNO only** (`ocp-sno`), gated behind `TROSHKA_LIVE_TIER2=1` + a configured pull secret.
- **PROCESS GUARD (all implementers):** NEVER `git stash`/`stash pop`/`stash apply`; only `git add <paths>` + commit; STOP+report on unexpected `git status`. There are 2 pre-existing stashes on `main` — leave them.
- **Verified reference facts (do not re-derive):** API base `/api/v1`; `POST /projects/from-template` (dict body: `template_id`,`name`,`install_via`,`auto_install_ocp`,`ocp_version`,`bastion_image_id`,`bastion_iso_id`,`common_password`,`auto_deploy`); `POST /projects/{id}/deploy` (needs `state=="draft"`); `POST /projects/{id}/undeploy`; `DELETE /projects/{id}`; `GET /projects/{id}` → `state`,`ocp_status`,`ocp_status_detail`,`ocp_install_elapsed`; `GET /projects/templates`. `ocp_status`: `None→"monitoring"→"ready"|"warning"` (success) or `"error"` (fail). Auth: `Authorization: Bearer trk_…`, or none in dev-mode. Scoped key row: `ApiKey.name == f"ops-pod:{project_id}"`, `scopes==["topology:read","vm:exec"]`, `is_active`. Ops-pod name: troshkad container `troshka-<pid8>-ops`; kubevirt pod `troshka-<pid8>-ops` in namespace `troshka-<pid8>`. SNO template id `ocp-sno`, `category=="openshift"`.

**Import-robustness note (applies to all tasks):** the live tree uses **flat, `live_`-prefixed modules** (`live_config.py`, `live_api.py`, `live_hostcmd.py`, `live_scopedkey.py`) imported by bare name, made importable by a `sys.path` bootstrap at the top of `tests/live/conftest.py`. This avoids `tests/__init__.py` (which would change how existing tests are collected) and avoids a `config`/`helpers` top-level name collision. The parent `tests/conftest.py` (SQLite) auto-applies to this subdir but is inert here — live tests never use its DB session; helpers must NOT import `app.*` in-process (they shell out).

---

### Task 1: Markers + live config + skip guard (foundation)

**Files:**
- Modify: `src/backend/pyproject.toml` (`[tool.pytest.ini_options]` — add `markers`)
- Create: `src/backend/tests/live/live_config.py`
- Create: `src/backend/tests/live/conftest.py`
- Test: `src/backend/tests/live/test_live_config.py`

**Interfaces:**
- Produces: `LiveConfig` dataclass with `from_env(env=None) -> LiveConfig`; properties `configured: bool`, `troshkad_ready: bool`, `kubevirt_ready: bool`; fields `url, token, troshkad_host, kubeconfig, kubevirt_host, tier2_enabled: bool, timeout_s: int`. `conftest.py` provides the collection skip-guard for markers `live_env`/`live_troshkad`/`live_kubevirt`/`tier2`.

- [ ] **Step 1: Write the failing test** — `src/backend/tests/live/test_live_config.py`

```python
from live_config import LiveConfig


def test_unconfigured_when_no_url():
    cfg = LiveConfig.from_env({})
    assert cfg.configured is False
    assert cfg.troshkad_ready is False
    assert cfg.kubevirt_ready is False
    assert cfg.tier2_enabled is False
    assert cfg.timeout_s == 4200


def test_full_env_parsed():
    cfg = LiveConfig.from_env(
        {
            "TROSHKA_LIVE_URL": "http://localhost:8200",
            "TROSHKA_LIVE_TOKEN": "trk_abc",
            "TROSHKA_LIVE_TROSHKAD_HOST": "a1b2",
            "TROSHKA_LIVE_KUBECONFIG": "/tmp/kc",
            "TROSHKA_LIVE_KUBEVIRT_HOST": "c3d4",
            "TROSHKA_LIVE_TIER2": "1",
            "TROSHKA_LIVE_TIMEOUT_S": "600",
        }
    )
    assert cfg.configured
    assert cfg.token == "trk_abc"
    assert cfg.troshkad_ready
    assert cfg.kubevirt_ready
    assert cfg.tier2_enabled
    assert cfg.timeout_s == 600


def test_kubevirt_needs_both_kubeconfig_and_host():
    cfg = LiveConfig.from_env(
        {"TROSHKA_LIVE_URL": "u", "TROSHKA_LIVE_KUBECONFIG": "/tmp/kc"}
    )
    assert cfg.kubevirt_ready is False  # host missing


def test_empty_strings_treated_as_unset():
    cfg = LiveConfig.from_env({"TROSHKA_LIVE_URL": ""})
    assert cfg.configured is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/live/test_live_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'live_config'` (the `conftest.py` sys.path bootstrap from Step 3 makes it importable).

- [ ] **Step 3: Create `src/backend/tests/live/live_config.py`**

```python
"""Env-var-driven configuration + skip decisions for the live-env harness."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class LiveConfig:
    url: str | None
    token: str | None
    troshkad_host: str | None
    kubeconfig: str | None
    kubevirt_host: str | None
    tier2_enabled: bool
    timeout_s: int

    @classmethod
    def from_env(cls, env: dict | None = None) -> "LiveConfig":
        env = os.environ if env is None else env

        def val(key: str) -> str | None:
            return env.get(key) or None

        return cls(
            url=val("TROSHKA_LIVE_URL"),
            token=val("TROSHKA_LIVE_TOKEN"),
            troshkad_host=val("TROSHKA_LIVE_TROSHKAD_HOST"),
            kubeconfig=val("TROSHKA_LIVE_KUBECONFIG"),
            kubevirt_host=val("TROSHKA_LIVE_KUBEVIRT_HOST"),
            tier2_enabled=env.get("TROSHKA_LIVE_TIER2") == "1",
            timeout_s=int(env.get("TROSHKA_LIVE_TIMEOUT_S") or "4200"),
        )

    @property
    def configured(self) -> bool:
        return bool(self.url)

    @property
    def troshkad_ready(self) -> bool:
        return bool(self.troshkad_host)

    @property
    def kubevirt_ready(self) -> bool:
        return bool(self.kubeconfig and self.kubevirt_host)
```

- [ ] **Step 4: Create `src/backend/tests/live/conftest.py` (bootstrap + skip guard)**

```python
"""Live-env harness: sys.path bootstrap, collection skip-guard, fixtures.

Fixtures are added in Task 4; this task establishes import bootstrap + skips.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))  # make live_* modules importable

import pytest  # noqa: E402

from live_config import LiveConfig  # noqa: E402


def pytest_configure(config):
    for line in (
        "live_env: requires a running Troshka instance (TROSHKA_LIVE_URL)",
        "live_troshkad: requires a troshkad/libvirt host",
        "live_kubevirt: requires a kubevirt-cluster host",
        "tier2: slow real OCP install (~30-60 min)",
    ):
        config.addinivalue_line("markers", line)


def pytest_collection_modifyitems(config, items):
    cfg = LiveConfig.from_env()
    for item in items:
        if "live_env" not in item.keywords:
            continue
        if not cfg.configured:
            item.add_marker(pytest.mark.skip(reason="TROSHKA_LIVE_URL not set"))
            continue
        if "live_troshkad" in item.keywords and not cfg.troshkad_ready:
            item.add_marker(
                pytest.mark.skip(reason="TROSHKA_LIVE_TROSHKAD_HOST not set")
            )
        if "live_kubevirt" in item.keywords and not cfg.kubevirt_ready:
            item.add_marker(
                pytest.mark.skip(reason="kubevirt env (KUBECONFIG+HOST) not set")
            )
        if "tier2" in item.keywords and not cfg.tier2_enabled:
            item.add_marker(pytest.mark.skip(reason="TROSHKA_LIVE_TIER2 != 1"))
```

- [ ] **Step 5: Register markers in `src/backend/pyproject.toml`**

In `[tool.pytest.ini_options]` (currently only `testpaths = ["tests"]`), add:

```toml
markers = [
    "live_env: requires a running Troshka instance (TROSHKA_LIVE_URL)",
    "live_troshkad: requires a troshkad/libvirt host",
    "live_kubevirt: requires a kubevirt-cluster host",
    "tier2: slow real OCP install (~30-60 min)",
]
```

(The `pytest_configure` addinivalue_line in conftest is a belt-and-suspenders duplicate so a stray invocation from another rootdir still registers them.)

- [ ] **Step 6: Run tests + skip-guard verification**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/live/test_live_config.py -v`
Expected: 4 PASS.
Run (no live env): `cd src/backend && ./venv/bin/python3 -m pytest tests/live -v`
Expected: the config unit tests PASS; no `live_env` tests exist yet so none skipped — that's fine. No errors/warnings about unknown markers.

- [ ] **Step 7: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/tests/live/live_config.py src/backend/tests/live/conftest.py src/backend/tests/live/test_live_config.py src/backend/pyproject.toml && git commit -m "test(live): live-env harness config + marker skip-guard (Task 1)"
```

---

### Task 2: API helper — LiveClient + poll_ocp

**Files:**
- Create: `src/backend/tests/live/live_api.py`
- Test: `src/backend/tests/live/test_live_api.py`

**Interfaces:**
- Consumes: `LiveConfig` (Task 1).
- Produces: `LiveClient(base_url, token=None)` with `.get_json(path) -> dict`, `.post_json(path, body) -> httpx.Response`, `.delete(path) -> httpx.Response`, `.status(pid) -> dict` (GET `/api/v1/projects/{pid}`), and `.raw` (the `httpx.Client`). Module fn `poll_ocp(client, pid, until, timeout_s, interval_s=15) -> dict` returning the final status dict; `until` is a set like `{"ready","warning"}`; raises `TimeoutError` on timeout and `AssertionError` if it reaches `"error"` while `"error"` not in `until`.

- [ ] **Step 1: Confirm httpx present**

Run: `cd src/backend && ./venv/bin/python3 -c "import httpx; print(httpx.__version__)"`
Expected: prints a version. If it errors, STOP and report (do not add a dependency without approval; fall back plan is `requests`, but confirm first).

- [ ] **Step 2: Write the failing test** — `src/backend/tests/live/test_live_api.py`

```python
import httpx
import pytest

from live_api import LiveClient, poll_ocp


def _client_with(responses):
    """LiveClient whose transport replays queued (status_code, json) tuples."""
    seq = list(responses)

    def handler(request):
        code, payload = seq.pop(0)
        return httpx.Response(code, json=payload)

    c = LiveClient("http://t", token="trk_x")
    c.raw = httpx.Client(
        base_url="http://t",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer trk_x"},
    )
    return c


def test_client_sets_bearer_when_token():
    captured = {}

    def handler(request):
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ok": True})

    c = LiveClient("http://t", token="trk_x")
    c.raw = httpx.Client(
        base_url="http://t",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer trk_x"},
    )
    c.get_json("/api/v1/projects/p1")
    assert captured["auth"] == "Bearer trk_x"


def test_poll_ocp_returns_on_ready():
    c = _client_with(
        [
            (200, {"ocp_status": None}),
            (200, {"ocp_status": "monitoring", "ocp_install_elapsed": 10}),
            (200, {"ocp_status": "ready", "ocp_install_elapsed": 42}),
        ]
    )
    final = poll_ocp(c, "p1", until={"ready", "warning"}, timeout_s=100, interval_s=0)
    assert final["ocp_status"] == "ready"
    assert final["ocp_install_elapsed"] == 42


def test_poll_ocp_raises_on_unexpected_error():
    c = _client_with([(200, {"ocp_status": "monitoring"}), (200, {"ocp_status": "error"})])
    with pytest.raises(AssertionError):
        poll_ocp(c, "p1", until={"ready", "warning"}, timeout_s=100, interval_s=0)


def test_poll_ocp_times_out():
    c = _client_with([(200, {"ocp_status": "monitoring"})] * 50)
    with pytest.raises(TimeoutError):
        poll_ocp(c, "p1", until={"ready"}, timeout_s=0, interval_s=0)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/live/test_live_api.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'live_api'`.

- [ ] **Step 4: Create `src/backend/tests/live/live_api.py`**

```python
"""Thin HTTP client + OCP status poller for the live-env harness."""

from __future__ import annotations

import time

import httpx


class LiveClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 30.0):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.base_url = base_url
        self.raw = httpx.Client(base_url=base_url, headers=headers, timeout=timeout)

    def get_json(self, path: str) -> dict:
        r = self.raw.get(path)
        r.raise_for_status()
        return r.json()

    def post_json(self, path: str, body: dict) -> httpx.Response:
        return self.raw.post(path, json=body)

    def delete(self, path: str) -> httpx.Response:
        return self.raw.delete(path)

    def status(self, pid: str) -> dict:
        return self.get_json(f"/api/v1/projects/{pid}")

    def close(self) -> None:
        self.raw.close()


def poll_ocp(
    client: LiveClient,
    pid: str,
    until: set[str],
    timeout_s: float,
    interval_s: float = 15.0,
) -> dict:
    """Poll GET /projects/{pid} until ocp_status is in `until`.

    Returns the final status dict. Raises AssertionError if ocp_status becomes
    "error" while "error" not in `until`; raises TimeoutError past timeout_s.
    """
    deadline = time.monotonic() + timeout_s
    last: dict = {}
    while True:
        last = client.status(pid)
        st = last.get("ocp_status")
        if st in until:
            return last
        if st == "error" and "error" not in until:
            raise AssertionError(
                f"ocp_status became 'error': {last.get('ocp_status_detail')}"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"ocp_status={st!r} not in {until} after {timeout_s}s "
                f"(detail={last.get('ocp_status_detail')})"
            )
        time.sleep(interval_s)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/live/test_live_api.py -v`
Expected: 4 PASS.

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/tests/live/live_api.py src/backend/tests/live/test_live_api.py && git commit -m "test(live): LiveClient + poll_ocp helper (Task 2)"
```

---

### Task 3: Host-side helpers — host-ssh / oc / host-db wrappers

**Files:**
- Create: `src/backend/tests/live/live_hostcmd.py`
- Test: `src/backend/tests/live/test_live_hostcmd.py`

**Interfaces:**
- Produces: `REPO_ROOT` (Path to repo root); `host_ssh(prefix, *cmd) -> str`; `oc(*args, kubeconfig) -> str`; `host_db(python_src) -> str`; `ops_pod_apikey_row(project_id) -> dict|None` (runs host-db to read the `ops-pod:<pid>` row → `{"is_active": bool, "scopes": list}` or None). All raise `HostCmdError` on nonzero exit.

- [ ] **Step 1: Write the failing test** — `src/backend/tests/live/test_live_hostcmd.py`

```python
from unittest.mock import patch, MagicMock

import pytest

import live_hostcmd as hc


def _ok(stdout=""):
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def test_host_ssh_builds_command():
    with patch("live_hostcmd.subprocess.run", return_value=_ok("ok")) as run:
        out = hc.host_ssh("a1b2", "podman", "ps")
    assert out == "ok"
    argv = run.call_args[0][0]
    assert argv[0].endswith("scripts/host-ssh.sh")
    assert argv[1:] == ["a1b2", "podman", "ps"]


def test_oc_injects_kubeconfig():
    with patch("live_hostcmd.subprocess.run", return_value=_ok("pods")) as run:
        hc.oc("get", "pod", kubeconfig="/tmp/kc")
    argv = run.call_args[0][0]
    assert argv[0] == "oc"
    assert "--kubeconfig=/tmp/kc" in argv
    assert argv[-2:] == ["get", "pod"]


def test_host_cmd_raises_on_nonzero():
    with patch(
        "live_hostcmd.subprocess.run",
        return_value=MagicMock(returncode=1, stdout="", stderr="boom"),
    ):
        with pytest.raises(hc.HostCmdError):
            hc.host_ssh("a1b2", "false")


def test_ops_pod_apikey_row_parses_json():
    payload = '{"is_active": true, "scopes": ["topology:read", "vm:exec"]}'
    with patch("live_hostcmd.subprocess.run", return_value=_ok(payload)):
        row = hc.ops_pod_apikey_row("proj-1234")
    assert row == {"is_active": True, "scopes": ["topology:read", "vm:exec"]}


def test_ops_pod_apikey_row_none_when_absent():
    with patch("live_hostcmd.subprocess.run", return_value=_ok("null")):
        assert hc.ops_pod_apikey_row("proj-1234") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/live/test_live_hostcmd.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'live_hostcmd'`.

- [ ] **Step 3: Create `src/backend/tests/live/live_hostcmd.py`**

```python
"""Subprocess wrappers for host-side assertions (host-ssh / oc / host-db)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]  # tests/live -> backend -> src -> repo
_HOST_SSH = str(REPO_ROOT / "scripts" / "host-ssh.sh")
_HOST_DB = str(REPO_ROOT / "scripts" / "host-db.sh")


class HostCmdError(RuntimeError):
    pass


def _run(argv: list[str]) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise HostCmdError(f"{argv!r} exited {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


def host_ssh(prefix: str, *cmd: str) -> str:
    return _run([_HOST_SSH, prefix, *cmd])


def oc(*args: str, kubeconfig: str) -> str:
    return _run(["oc", f"--kubeconfig={kubeconfig}", *args])


def host_db(python_src: str) -> str:
    return _run([_HOST_DB, python_src])


def ops_pod_apikey_row(project_id: str) -> dict | None:
    """Return {'is_active', 'scopes'} for the ops-pod:<pid> key, or None."""
    src = (
        "import json;"
        "from app.models.api_key import ApiKey;"
        f"k=db.query(ApiKey).filter_by(name='ops-pod:{project_id}')"
        ".order_by(ApiKey.created_at.desc()).first();"
        "print(json.dumps(None if k is None else "
        "{'is_active': bool(k.is_active), 'scopes': k.scopes}))"
    )
    out = host_db(src).strip().splitlines()[-1]
    return json.loads(out)
```

> NOTE for implementer: verify `REPO_ROOT` resolves to the repo root — `tests/live/live_hostcmd.py` → parents[0]=`live`, [1]=`tests`, [2]=`backend`, [3]=`src`, [4]=repo. Confirm `(REPO_ROOT / "scripts" / "host-ssh.sh").exists()` in the test if unsure; adjust the index only if the file layout differs.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/live/test_live_hostcmd.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Add a REPO_ROOT sanity assertion to the test and re-run**

Append to `test_live_hostcmd.py`:

```python
def test_repo_root_points_at_scripts():
    assert (hc.REPO_ROOT / "scripts" / "host-ssh.sh").exists()
    assert (hc.REPO_ROOT / "scripts" / "host-db.sh").exists()
```

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/live/test_live_hostcmd.py -v`
Expected: 6 PASS.

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/tests/live/live_hostcmd.py src/backend/tests/live/test_live_hostcmd.py && git commit -m "test(live): host-ssh/oc/host-db subprocess wrappers (Task 3)"
```

---

### Task 4: Fixtures + scoped-key helper

**Files:**
- Create: `src/backend/tests/live/live_scopedkey.py`
- Modify: `src/backend/tests/live/conftest.py` (add fixtures)
- Test: `src/backend/tests/live/test_live_scopedkey.py`

**Interfaces:**
- Consumes: `LiveConfig` (T1), `LiveClient` (T2), `host_ssh`/`oc` (T3).
- Produces fixtures: `live_config`, `client` (session `LiveClient`), `ocp_template` (str id), `project_factory` (callable `create(**body) -> pid`, auto-DELETE teardown). Produces module fns in `live_scopedkey.py`: `scoped_key_from_pod(cfg, pid, provider) -> str` (reads `TROSHKA_API_KEY` env from the running ops pod) and `client_for_key(base_url, raw_key) -> LiveClient`.

- [ ] **Step 1: Write the failing test** — `src/backend/tests/live/test_live_scopedkey.py` (unit-tests the pure parts)

```python
from unittest.mock import patch

from live_config import LiveConfig
from live_scopedkey import client_for_key, scoped_key_from_pod


def test_client_for_key_sets_bearer():
    c = client_for_key("http://t", "trk_scoped")
    assert c.raw.headers.get("authorization") == "Bearer trk_scoped"


def test_scoped_key_from_pod_troshkad_uses_host_ssh():
    cfg = LiveConfig.from_env(
        {"TROSHKA_LIVE_URL": "u", "TROSHKA_LIVE_TROSHKAD_HOST": "a1b2"}
    )
    with patch(
        "live_scopedkey.host_ssh", return_value="trk_frompod\n"
    ) as hs:
        key = scoped_key_from_pod(cfg, "abcd1234-xxxx", provider="troshkad")
    assert key == "trk_frompod"
    argv = hs.call_args[0]
    assert argv[0] == "a1b2"
    assert "podman" in argv and "exec" in argv
    assert "troshka-abcd1234-ops" in argv  # container name


def test_scoped_key_from_pod_kubevirt_uses_oc_exec():
    cfg = LiveConfig.from_env(
        {
            "TROSHKA_LIVE_URL": "u",
            "TROSHKA_LIVE_KUBECONFIG": "/tmp/kc",
            "TROSHKA_LIVE_KUBEVIRT_HOST": "c3d4",
        }
    )
    with patch("live_scopedkey.oc", return_value="trk_kv\n") as oc_mock:
        key = scoped_key_from_pod(cfg, "abcd1234-xxxx", provider="kubevirt")
    assert key == "trk_kv"
    args = oc_mock.call_args
    assert args.kwargs["kubeconfig"] == "/tmp/kc"
    flat = " ".join(args[0])
    assert "exec" in flat and "troshka-abcd1234-ops" in flat
    assert "-n" in args[0] and "troshka-abcd1234" in args[0]  # namespace
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/live/test_live_scopedkey.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'live_scopedkey'`.

- [ ] **Step 3: Create `src/backend/tests/live/live_scopedkey.py`**

```python
"""Fetch + use the ops-pod scoped API key for least-privilege assertions."""

from __future__ import annotations

from live_api import LiveClient
from live_hostcmd import host_ssh, oc

_ENV_VAR = "TROSHKA_API_KEY"


def _pid8(pid: str) -> str:
    return pid[:8]


def scoped_key_from_pod(cfg, pid: str, provider: str) -> str:
    """Read the ops pod's own TROSHKA_API_KEY (the scoped trk_ key)."""
    container = f"troshka-{_pid8(pid)}-ops"
    if provider == "kubevirt":
        ns = f"troshka-{_pid8(pid)}"
        out = oc(
            "exec", "-n", ns, container, "--",
            "printenv", _ENV_VAR,
            kubeconfig=cfg.kubeconfig,
        )
    else:
        out = host_ssh(
            cfg.troshkad_host, "podman", "exec", container, "printenv", _ENV_VAR
        )
    return out.strip().splitlines()[-1].strip()


def client_for_key(base_url: str, raw_key: str) -> LiveClient:
    return LiveClient(base_url, token=raw_key)
```

- [ ] **Step 4: Add fixtures to `src/backend/tests/live/conftest.py`**

Append (after the existing skip-guard code):

```python
from live_api import LiveClient  # noqa: E402


@pytest.fixture(scope="session")
def live_config():
    return LiveConfig.from_env()


@pytest.fixture(scope="session")
def client(live_config):
    if not live_config.configured:
        pytest.skip("TROSHKA_LIVE_URL not set")
    c = LiveClient(live_config.url, token=live_config.token)
    yield c
    c.close()


@pytest.fixture(scope="session")
def ocp_template(client):
    templates = client.get_json("/api/v1/projects/templates")
    ids = [t["id"] for t in templates if t.get("category") == "openshift"]
    assert "ocp-sno" in ids, f"ocp-sno template missing; found {ids}"
    return "ocp-sno"


@pytest.fixture
def project_factory(client):
    created: list[str] = []

    def create(**body):
        payload = {"auto_install_ocp": True, "install_via": "pod", **body}
        r = client.post_json("/api/v1/projects/from-template", payload)
        assert r.status_code == 201, f"from-template failed: {r.status_code} {r.text}"
        pid = r.json()["id"]
        created.append(pid)
        return pid

    yield create
    for pid in created:
        try:
            client.delete(f"/api/v1/projects/{pid}")
        except Exception:
            pass
```

- [ ] **Step 5: Run tests to verify pass + no collection breakage**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/live/test_live_scopedkey.py -v`
Expected: 3 PASS.
Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/live --collect-only -q`
Expected: collects the unit tests + (currently no live_env tests). No errors.

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/tests/live/live_scopedkey.py src/backend/tests/live/conftest.py src/backend/tests/live/test_live_scopedkey.py && git commit -m "test(live): fixtures (client/template/project_factory) + scoped-key helper (Task 4)"
```

---

### Task 5: Tier-1 ops-pod tests (exists / running / no-secret-in-argv)

**Files:**
- Create: `src/backend/tests/live/test_tier1_ops_pod.py`
- Test: same file (these ARE the tests; verified by clean collection + skip).

**Interfaces:**
- Consumes: `client`, `project_factory`, `live_config` fixtures; `host_ssh`/`oc` (T3). A local helper `_wait_ops_pod_created(...)` polls until the ops pod appears.

- [ ] **Step 1: Write the live tests** — `src/backend/tests/live/test_tier1_ops_pod.py`

```python
"""Tier-1: the ops pod is created for a pod OCP project, with no secrets in argv.

These are live_env tests: they require a running Troshka + a real host. Without
TROSHKA_LIVE_* they are skipped by the conftest collection guard.
"""

import time

import pytest

from live_hostcmd import host_ssh, oc

SECRET_MARKERS = ("BEGIN CERTIFICATE", "pullSecret", "\"auth\"", "-----BEGIN")


def _ops_container(pid):
    return f"troshka-{pid[:8]}-ops"


def _wait_ops_pod_troshkad(cfg, pid, timeout=900):
    name = _ops_container(pid)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = host_ssh(cfg.troshkad_host, "podman", "ps", "--format", "{{.Names}}")
        if any(name in line for line in out.splitlines()):
            return name
        time.sleep(15)
    pytest.fail(f"ops pod {name} never appeared within {timeout}s")


def _wait_ops_pod_kubevirt(cfg, pid, timeout=900):
    name = _ops_container(pid)
    ns = f"troshka-{pid[:8]}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = oc("get", "pod", name, "-n", ns, "--no-headers",
                 "--ignore-not-found", kubeconfig=cfg.kubeconfig)
        if name in out:
            return name, ns
        time.sleep(15)
    pytest.fail(f"ops pod {name} never appeared in {ns} within {timeout}s")


@pytest.mark.live_env
@pytest.mark.live_troshkad
def test_troshkad_ops_pod_created_and_running(live_config, client, project_factory, ocp_template):
    pid = project_factory(template_id=ocp_template, name="live-t1-troshkad", auto_deploy=True)
    name = _wait_ops_pod_troshkad(live_config, pid)
    inspect = host_ssh(live_config.troshkad_host, "podman", "inspect", name)
    assert '"Running": true' in inspect or '"Status": "running"' in inspect


@pytest.mark.live_env
@pytest.mark.live_troshkad
def test_troshkad_no_secret_in_argv(live_config, client, project_factory, ocp_template):
    pid = project_factory(template_id=ocp_template, name="live-t1-nosecret", auto_deploy=True)
    name = _wait_ops_pod_troshkad(live_config, pid)
    inspect = host_ssh(
        live_config.troshkad_host, "podman", "inspect",
        "--format", "{{.Config.CreateCommand}} {{.Args}} {{.Config.Cmd}}", name,
    )
    for marker in SECRET_MARKERS:
        assert marker not in inspect, f"secret marker {marker!r} leaked into argv"


@pytest.mark.live_env
@pytest.mark.live_kubevirt
def test_kubevirt_ops_pod_created_and_running(live_config, client, project_factory, ocp_template):
    pid = project_factory(template_id=ocp_template, name="live-t1-kv", auto_deploy=True)
    name, ns = _wait_ops_pod_kubevirt(live_config, pid)
    phase = oc("get", "pod", name, "-n", ns, "-o",
               "jsonpath={.status.phase}", kubeconfig=live_config.kubeconfig)
    assert phase.strip() == "Running"


@pytest.mark.live_env
@pytest.mark.live_kubevirt
def test_kubevirt_configs_are_secret_mount_not_argv(live_config, client, project_factory, ocp_template):
    pid = project_factory(template_id=ocp_template, name="live-t1-kvsecret", auto_deploy=True)
    name, ns = _wait_ops_pod_kubevirt(live_config, pid)
    # The install-config / pull-secret ride in a mounted Secret volume, not argv.
    vols = oc("get", "pod", name, "-n", ns, "-o",
              "jsonpath={.spec.volumes[*].secret.secretName}", kubeconfig=live_config.kubeconfig)
    assert vols.strip(), "expected a mounted secret volume on the ops pod"
    cmd = oc("get", "pod", name, "-n", ns, "-o",
             "jsonpath={.spec.containers[*].command}", kubeconfig=live_config.kubeconfig)
    for marker in SECRET_MARKERS:
        assert marker not in cmd
```

- [ ] **Step 2: Verify clean collection + skip (no live env)**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/live/test_tier1_ops_pod.py -v`
Expected: 4 tests, all SKIPPED with reason "TROSHKA_LIVE_URL not set". No errors, no unknown-marker warnings.

- [ ] **Step 3: Verify marker selection works**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/live -m "live_env and live_troshkad" --collect-only -q`
Expected: lists the 2 troshkad tests (collection only; they'd skip without env).

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/tests/live/test_tier1_ops_pod.py && git commit -m "test(live): Tier-1 ops-pod exists/running/no-secret-in-argv (Task 5)"
```

---

### Task 6: Tier-1 security tests (scoped-key least-privilege + revoke + cancel)

**Files:**
- Create: `src/backend/tests/live/test_tier1_security.py`

**Interfaces:**
- Consumes: `client`, `project_factory`, `live_config`, `ocp_template`; `scoped_key_from_pod`/`client_for_key` (T4), `ops_pod_apikey_row` (T3), the `_wait_ops_pod_*` waiters (duplicate the small waiter locally or import from Task 5 module).

- [ ] **Step 1: Write the live tests** — `src/backend/tests/live/test_tier1_security.py`

```python
"""Tier-1 security: scoped ops-pod key least-privilege, active-then-revoked."""

import pytest

from live_hostcmd import host_ssh, oc, ops_pod_apikey_row
from live_scopedkey import client_for_key, scoped_key_from_pod
from test_tier1_ops_pod import _wait_ops_pod_troshkad, _wait_ops_pod_kubevirt


def _params():
    return [
        pytest.param("troshkad", marks=pytest.mark.live_troshkad, id="troshkad"),
        pytest.param("kubevirt", marks=pytest.mark.live_kubevirt, id="kubevirt"),
    ]


def _wait_ops(provider, cfg, pid):
    if provider == "kubevirt":
        _wait_ops_pod_kubevirt(cfg, pid)
    else:
        _wait_ops_pod_troshkad(cfg, pid)


@pytest.mark.live_env
@pytest.mark.parametrize("provider", _params())
def test_scoped_key_least_privilege(provider, live_config, client, project_factory, ocp_template):
    pid = project_factory(template_id=ocp_template, name=f"live-sec-{provider}", auto_deploy=True)
    _wait_ops(provider, live_config, pid)

    raw = scoped_key_from_pod(live_config, pid, provider=provider)
    scoped = client_for_key(live_config.url, raw)

    # allowlisted + own project: 200
    assert scoped.raw.get(f"/api/v1/projects/{pid}").status_code == 200
    # non-allowlisted routes on own project: 403
    assert scoped.raw.post(f"/api/v1/projects/{pid}/deploy").status_code == 403
    assert scoped.raw.delete(f"/api/v1/projects/{pid}").status_code == 403
    # someone else's project (any other id): 403
    other = "00000000-0000-0000-0000-000000000000"
    assert scoped.raw.get(f"/api/v1/projects/{other}").status_code == 403


@pytest.mark.live_env
@pytest.mark.live_troshkad
def test_scoped_key_active_then_revoked_on_destroy(live_config, client, project_factory, ocp_template):
    pid = project_factory(template_id=ocp_template, name="live-sec-revoke", auto_deploy=True)
    _wait_ops_pod_troshkad(live_config, pid)

    row = ops_pod_apikey_row(pid)
    assert row is not None and row["is_active"] is True
    assert row["scopes"] == ["topology:read", "vm:exec"]

    assert client.delete(f"/api/v1/projects/{pid}").status_code in (200, 204)
    # After DELETE the project (and cascade) is gone → row absent, or is_active False.
    row2 = ops_pod_apikey_row(pid)
    assert row2 is None or row2["is_active"] is False


@pytest.mark.live_env
@pytest.mark.live_troshkad
def test_cancel_deletes_ops_pod(live_config, client, project_factory, ocp_template):
    pid = project_factory(template_id=ocp_template, name="live-sec-cancel", auto_deploy=True)
    name = _wait_ops_pod_troshkad(live_config, pid)
    assert client.post_json(f"/api/v1/projects/{pid}/undeploy", {}).status_code in (200, 204)
    out = host_ssh(live_config.troshkad_host, "podman", "ps", "-a", "--format", "{{.Names}}")
    assert name not in out.split(), f"ops pod {name} still present after undeploy"
```

- [ ] **Step 2: Verify clean collection + skip**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/live/test_tier1_security.py -v`
Expected: all tests SKIPPED (no live env). No import errors (the cross-module import of the waiters resolves via the sys.path bootstrap).

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/tests/live/test_tier1_security.py && git commit -m "test(live): Tier-1 scoped-key least-privilege + revoke + cancel (Task 6)"
```

---

### Task 7: Tier-1 bastion regression (no ops pod)

**Files:**
- Create: `src/backend/tests/live/test_tier1_bastion.py`

- [ ] **Step 1: Write the live test** — `src/backend/tests/live/test_tier1_bastion.py`

```python
"""Tier-1: an install_via=bastion project bakes a bastion, creates NO ops pod."""

import time

import pytest

from live_hostcmd import host_ssh


@pytest.mark.live_env
@pytest.mark.live_troshkad
def test_bastion_project_creates_no_ops_pod(live_config, client, project_factory, ocp_template):
    pid = project_factory(
        template_id=ocp_template,
        name="live-bastion",
        install_via="bastion",
        auto_deploy=True,
    )
    ops_name = f"troshka-{pid[:8]}-ops"
    # Give the deploy time to reach VM-start; the bastion path must never create
    # an ops pod. Sample a few times to be sure it doesn't appear.
    for _ in range(8):
        out = host_ssh(live_config.troshkad_host, "podman", "ps", "-a", "--format", "{{.Names}}")
        assert ops_name not in out.split(), "bastion project must not create an ops pod"
        time.sleep(15)
```

- [ ] **Step 2: Verify clean collection + skip**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/live/test_tier1_bastion.py -v`
Expected: 1 test SKIPPED (no live env).

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/tests/live/test_tier1_bastion.py && git commit -m "test(live): Tier-1 bastion makes no ops pod (Task 7)"
```

---

### Task 8: Tier-2 real SNO install → ready (both providers)

**Files:**
- Create: `src/backend/tests/live/test_tier2_install.py`

**Interfaces:**
- Consumes: `client`, `project_factory`, `live_config`, `ocp_template`; `poll_ocp` (T2).

- [ ] **Step 1: Write the live tests** — `src/backend/tests/live/test_tier2_install.py`

```python
"""Tier-2: a real (pod) SNO install reaches ready on troshkad and kubevirt.

Slow (~30-60 min). Gated by TROSHKA_LIVE_TIER2=1 and a configured pull secret.
"""

import pytest

from live_api import poll_ocp


def _params():
    return [
        pytest.param("troshkad", marks=pytest.mark.live_troshkad, id="troshkad"),
        pytest.param("kubevirt", marks=pytest.mark.live_kubevirt, id="kubevirt"),
    ]


@pytest.mark.live_env
@pytest.mark.tier2
@pytest.mark.parametrize("provider", _params())
def test_pod_sno_install_reaches_ready(provider, live_config, client, project_factory, ocp_template):
    pid = project_factory(
        template_id=ocp_template, name=f"live-t2-{provider}", auto_deploy=True
    )
    final = poll_ocp(
        client, pid, until={"ready", "warning"},
        timeout_s=live_config.timeout_s, interval_s=30,
    )
    assert final["ocp_status"] in ("ready", "warning")
    assert (final.get("ocp_install_elapsed") or 0) > 0


@pytest.mark.live_env
@pytest.mark.tier2
@pytest.mark.live_troshkad
def test_monitor_reports_progress(live_config, client, project_factory, ocp_template):
    pid = project_factory(template_id=ocp_template, name="live-t2-progress", auto_deploy=True)
    # Sample once early: status should be monitoring with a detail string before ready.
    import time
    time.sleep(120)
    mid = client.status(pid)
    assert mid["ocp_status"] in ("monitoring", "ready", "warning")
    final = poll_ocp(client, pid, until={"ready", "warning"},
                     timeout_s=live_config.timeout_s, interval_s=30)
    assert final["ocp_status"] in ("ready", "warning")
```

- [ ] **Step 2: Verify clean collection + skip (tier2 gated)**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/live/test_tier2_install.py -v`
Expected: all SKIPPED (no live env → skip). With `TROSHKA_LIVE_URL` set but `TROSHKA_LIVE_TIER2` unset they'd also skip (tier2 guard) — confirm by:
Run: `cd src/backend && TROSHKA_LIVE_URL=http://x ./venv/bin/python3 -m pytest tests/live/test_tier2_install.py -v`
Expected: SKIPPED with reason mentioning tier2 (or the provider-host guard). No errors.

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/tests/live/test_tier2_install.py && git commit -m "test(live): Tier-2 pod SNO install reaches ready (Task 8)"
```

---

### Task 9: Tier-2 lifecycle (idempotency-restart + bastion install)

**Files:**
- Create: `src/backend/tests/live/test_tier2_lifecycle.py`

- [ ] **Step 1: Write the live tests** — `src/backend/tests/live/test_tier2_lifecycle.py`

```python
"""Tier-2 lifecycle: idempotent restart (troshkad), bastion install unchanged."""

import time

import pytest

from live_api import poll_ocp
from live_hostcmd import host_ssh
from test_tier1_ops_pod import _wait_ops_pod_troshkad


@pytest.mark.live_env
@pytest.mark.tier2
@pytest.mark.live_troshkad
def test_idempotent_restart_skips_completed_cluster(live_config, client, project_factory, ocp_template):
    pid = project_factory(template_id=ocp_template, name="live-t2-restart", auto_deploy=True)
    name = _wait_ops_pod_troshkad(live_config, pid)
    # Let the install get underway, then kill the ops pod; restart_policy=always
    # brings it back and the per-cluster kubeconfig skip-guard must avoid a rerun.
    time.sleep(300)
    host_ssh(live_config.troshkad_host, "podman", "restart", name)
    final = poll_ocp(client, pid, until={"ready", "warning"},
                     timeout_s=live_config.timeout_s, interval_s=30)
    assert final["ocp_status"] in ("ready", "warning")


@pytest.mark.live_env
@pytest.mark.tier2
@pytest.mark.live_troshkad
def test_bastion_sno_install_unchanged(live_config, client, project_factory, ocp_template):
    pid = project_factory(
        template_id=ocp_template, name="live-t2-bastion",
        install_via="bastion", auto_deploy=True,
    )
    final = poll_ocp(client, pid, until={"ready", "warning"},
                     timeout_s=live_config.timeout_s, interval_s=30)
    assert final["ocp_status"] in ("ready", "warning")
```

- [ ] **Step 2: Verify clean collection + skip**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/live/test_tier2_lifecycle.py -v`
Expected: all SKIPPED (no live env). No import errors.

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/tests/live/test_tier2_lifecycle.py && git commit -m "test(live): Tier-2 idempotent restart + bastion install (Task 9)"
```

---

### Task 10: README + invocation docs + final regression self-check

**Files:**
- Create: `src/backend/tests/live/README.md`
- Modify: `docs/superpowers/plans/2026-09-02-multi-cluster-ocp-plan4b-live-env-checklist.md` (add a header pointer to the automated harness)

- [ ] **Step 1: Write `src/backend/tests/live/README.md`**

Include: purpose (automates the Plan 4b [LIVE-ENV] checklist); the env-var table (from the spec §4.3); how to reach a deployed sandbox (`oc port-forward -n troshka svc/troshka-backend 8200:8200 --kubeconfig=<cfg>` then `export TROSHKA_LIVE_URL=http://localhost:8200`); auth (omit token for a dev instance, else `export TROSHKA_LIVE_TOKEN=trk_…`); invocation:

```bash
# Tier 1, both providers (fast):
TROSHKA_LIVE_URL=... TROSHKA_LIVE_TROSHKAD_HOST=... TROSHKA_LIVE_KUBECONFIG=... TROSHKA_LIVE_KUBEVIRT_HOST=... \
  ./venv/bin/python3 -m pytest tests/live -m "live_env and not tier2" -v
# Tier 2 (real install, ~30-60 min):
TROSHKA_LIVE_TIER2=1 TROSHKA_LIVE_URL=... ... \
  ./venv/bin/python3 -m pytest tests/live -m "live_env and tier2" -v
# One track only: add  -m "live_env and live_troshkad"  or  live_kubevirt
```

Document the pull-secret precondition for Tier 2 and expected runtimes; note that teardown is automatic (projects are always deleted).

- [ ] **Step 2: Add a pointer in the checklist doc**

Add near the top of `docs/superpowers/plans/2026-09-02-multi-cluster-ocp-plan4b-live-env-checklist.md`:

```markdown
> **Automated harness:** `src/backend/tests/live/` implements this checklist as pytest live_env tests
> (`pytest tests/live -m "live_env [and tier2]"`). See `src/backend/tests/live/README.md`.
```

- [ ] **Step 3: Final regression self-check**

Run (unit CI unaffected): `cd src/backend && ./venv/bin/python3 -m pytest -q`
Expected: full suite green; the live helper unit tests pass; every `live_env` test is SKIPPED (no env). Note the counts.
Run (marker sanity): `cd src/backend && ./venv/bin/python3 -m pytest tests/live -m live_env --collect-only -q`
Expected: collects all live tests across the 5 test files; no unknown-marker warnings.
Run: `cd src/backend && black --check tests/live && pyright tests/live`
Expected: black clean; pyright clean on the harness (the test modules import bare `live_*` names resolved at runtime via the conftest bootstrap — if pyright flags those as unresolved, add a `# pyright: reportMissingImports=false` header to the affected test files, matching repo convention for runtime-path imports).

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/tests/live/README.md docs/superpowers/plans/2026-09-02-multi-cluster-ocp-plan4b-live-env-checklist.md && git commit -m "docs(live): harness README + checklist pointer + final self-check (Task 10)"
```

---

## Self-Review

**Spec coverage:**
- §4.1 layout → Tasks 1–10 (flat `live_` module refinement noted for import robustness).
- §4.2 markers/skip → Task 1 (registration + `pytest_collection_modifyitems`).
- §4.3 config env vars → Task 1 (`LiveConfig`).
- §5 fixtures → Task 4.
- §6 assertion helpers → Tasks 2 (api), 3 (hostssh/oc/hostdb), 4 (scoped key).
- §7.1 Tier-1 catalog: ops-pod exists/running (T5), no-secret-in-argv (T5), scoped-key 403s (T6), key active+revoked (T6), cancel deletes pod (T6), bastion no-ops-pod (T7).
- §7.2 Tier-2 catalog: pod SNO→ready both providers (T8), monitor phases (T8), idempotency restart (T9), bastion install (T9).
- §8 safety/invocation → project_factory teardown (T4), README + invocation + final self-check (T10).
- Preconditions: Tier-2 skip-when-unconfigured (T1 guard + T8 gate); sandbox reach documented (T10 README).

**Placeholder scan:** every code step contains runnable code; every verification step has an exact command + expected result. No TBD/TODO.

**Type consistency:** `LiveConfig` fields/properties (T1) are used verbatim in T2/T4/T5–T9. `LiveClient` API (`get_json/post_json/delete/status/raw/close`) defined in T2, used consistently. `poll_ocp(client, pid, until, timeout_s, interval_s)` signature identical across T2/T8/T9. `host_ssh/oc/host_db/ops_pod_apikey_row` (T3) used verbatim in T5/T6/T7/T9. `scoped_key_from_pod(cfg, pid, provider)` / `client_for_key(base_url, raw_key)` (T4) used in T6. Ops-pod naming `troshka-<pid8>-ops` and ns `troshka-<pid8>` consistent everywhere.

**Deviation from spec (intentional, low-risk):** spec §4.1 showed a `helpers/` subpackage; the plan uses flat `live_`-prefixed modules to avoid `tests/__init__.py` collection changes and top-level name collisions. Same responsibilities, cleaner imports.
