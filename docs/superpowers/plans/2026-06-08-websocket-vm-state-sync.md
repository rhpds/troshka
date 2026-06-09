# WebSocket VM State Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all frontend polling for VM state, project state, and deploy progress with a per-project WebSocket that pushes changes in real-time.

**Architecture:** In-memory pub/sub dict (project_id → set of WebSocket connections) on the backend, with a shared `useVmStateSocket` React hook on the frontend. State changes are pushed both immediately after mutations (instant feedback) and via a 5-second background poller (catches external changes). Full state snapshots on connect/reconnect — no sequence numbers or deltas.

**Tech Stack:** FastAPI WebSocket, threading.Lock, asyncio event loop bridge, React hooks

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/backend/app/services/ws_pubsub.py` | **New** — in-memory pub/sub: subscribe/unsubscribe/notify, background state poller, async/sync bridge |
| `src/backend/app/api/ws.py` | **New** — WebSocket endpoint `/projects/{project_id}/ws`, auth, initial snapshot, heartbeat loop |
| `src/backend/app/api/projects.py` | **Modify** — add `notify_project()` calls after VM power actions and project state transitions |
| `src/backend/app/services/deploy_service.py` | **Modify** — add `notify_project()` calls on deploy progress updates and completion |
| `src/backend/app/main.py` | **Modify** — register WS router, start background state poller, capture event loop |
| `src/frontend/src/hooks/useVmStateSocket.ts` | **New** — shared WebSocket hook with reconnect + exponential backoff |
| `src/frontend/src/app/projects/[id]/page.tsx` | **Modify** — use hook, remove polling loops + localStorage listener |
| `src/frontend/src/app/console/page.tsx` | **Modify** — use hook, remove polling + localStorage writes |
| `src/backend/tests/test_ws_pubsub.py` | **New** — unit tests for pub/sub module |

---

### Task 1: Backend Pub/Sub Module

**Files:**
- Create: `src/backend/app/services/ws_pubsub.py`
- Test: `src/backend/tests/test_ws_pubsub.py`

This module manages WebSocket subscriber sets keyed by project_id and provides `notify_project()` to broadcast messages. It also runs the background state poller.

- [ ] **Step 1: Write failing test for subscribe/unsubscribe**

Create `src/backend/tests/test_ws_pubsub.py`:

```python
import asyncio
from unittest.mock import AsyncMock
from app.services.ws_pubsub import subscribe, unsubscribe, get_active_project_ids, _subscribers


def _make_ws():
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    return ws


def test_subscribe_and_unsubscribe():
    _subscribers.clear()
    ws = _make_ws()
    subscribe("proj-1", ws)
    assert "proj-1" in get_active_project_ids()
    unsubscribe("proj-1", ws)
    assert "proj-1" not in get_active_project_ids()


def test_subscribe_multiple():
    _subscribers.clear()
    ws1 = _make_ws()
    ws2 = _make_ws()
    subscribe("proj-1", ws1)
    subscribe("proj-1", ws2)
    assert len(_subscribers["proj-1"]) == 2
    unsubscribe("proj-1", ws1)
    assert len(_subscribers["proj-1"]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ws_pubsub.py -v`
Expected: FAIL with ImportError (module doesn't exist)

- [ ] **Step 3: Create ws_pubsub.py with subscribe/unsubscribe/get_active_project_ids**

Create `src/backend/app/services/ws_pubsub.py`:

```python
"""
In-memory WebSocket pub/sub for real-time project state updates.

Manages subscriber sets keyed by project_id. Sync callers (background threads,
deploy service) use notify_project() which bridges into the async event loop
via asyncio.run_coroutine_threadsafe().
"""
import asyncio
import json
import logging
import threading

from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)

_subscribers: dict[str, set[WebSocket]] = {}
_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None


def set_event_loop(loop: asyncio.AbstractEventLoop):
    global _loop
    _loop = loop


def subscribe(project_id: str, ws: WebSocket):
    with _lock:
        if project_id not in _subscribers:
            _subscribers[project_id] = set()
        _subscribers[project_id].add(ws)
    logger.debug("WS subscribe: project=%s (total=%d)", project_id[:8], len(_subscribers[project_id]))


def unsubscribe(project_id: str, ws: WebSocket):
    with _lock:
        subs = _subscribers.get(project_id)
        if subs:
            subs.discard(ws)
            if not subs:
                del _subscribers[project_id]


def get_active_project_ids() -> set[str]:
    with _lock:
        return set(_subscribers.keys())


async def _send_to_subscribers(project_id: str, message: dict):
    with _lock:
        subs = set(_subscribers.get(project_id, set()))

    data = json.dumps(message)
    dead = []
    for ws in subs:
        try:
            await ws.send_text(data)
        except Exception:
            dead.append(ws)

    if dead:
        with _lock:
            s = _subscribers.get(project_id)
            if s:
                for ws in dead:
                    s.discard(ws)
                if not s:
                    del _subscribers[project_id]


def notify_project(project_id: str, message: dict):
    if not _loop:
        return
    with _lock:
        if project_id not in _subscribers:
            return
    asyncio.run_coroutine_threadsafe(_send_to_subscribers(project_id, message), _loop)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ws_pubsub.py -v`
Expected: PASS

- [ ] **Step 5: Write test for notify_project**

Add to `src/backend/tests/test_ws_pubsub.py`:

```python
def test_notify_project():
    _subscribers.clear()
    loop = asyncio.new_event_loop()

    from app.services.ws_pubsub import set_event_loop, notify_project
    set_event_loop(loop)

    ws = _make_ws()
    subscribe("proj-1", ws)

    msg = {"type": "vm-state", "states": {"vm-1": "running"}}
    notify_project("proj-1", msg)

    # Run the event loop briefly to process the coroutine
    loop.run_until_complete(asyncio.sleep(0.1))
    ws.send_text.assert_called_once()
    sent = json.loads(ws.send_text.call_args[0][0])
    assert sent["type"] == "vm-state"
    assert sent["states"]["vm-1"] == "running"
    loop.close()


def test_notify_dead_connection_removed():
    _subscribers.clear()
    loop = asyncio.new_event_loop()

    from app.services.ws_pubsub import set_event_loop, notify_project
    set_event_loop(loop)

    ws = _make_ws()
    ws.send_text.side_effect = RuntimeError("connection closed")
    subscribe("proj-1", ws)

    notify_project("proj-1", {"type": "test"})
    loop.run_until_complete(asyncio.sleep(0.1))

    assert "proj-1" not in get_active_project_ids()
    loop.close()
```

Add `import json` to the top of the test file.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ws_pubsub.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/backend/app/services/ws_pubsub.py src/backend/tests/test_ws_pubsub.py
git commit -m "feat: add WebSocket pub/sub module for real-time project state"
```

---

### Task 2: WebSocket Endpoint

**Files:**
- Create: `src/backend/app/api/ws.py`
- Modify: `src/backend/app/main.py`

The WebSocket endpoint handles auth, sends an initial snapshot, and keeps the connection alive with heartbeats.

- [ ] **Step 1: Create the WebSocket endpoint**

Create `src/backend/app/api/ws.py`:

```python
"""WebSocket endpoint for real-time project state updates."""
import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.core.config import config
from app.core.database import SessionLocal
from app.models.project import Project
from app.models.user import User
from app.services.ws_pubsub import subscribe, unsubscribe

logger = logging.getLogger(__name__)

router = APIRouter()

HEARTBEAT_INTERVAL = 30


def _authenticate_ws(token: str | None, db) -> User | None:
    if not config.auth.oauth_enabled and not token:
        from app.core.auth import _get_or_create_dev_user
        return _get_or_create_dev_user(db)

    if not token:
        return None

    from app.core.auth import decode_jwt
    payload = decode_jwt(token)
    if not payload:
        return None
    email = payload.get("email") or payload.get("sub")
    if not email:
        return None
    return db.query(User).filter_by(email=email).first()


def _build_snapshot(project: Project, db) -> dict:
    from app.services.deploy_service import _deploy_progress
    from app.api.projects import _redeploy_progress, _domain_name
    from app.services.troshkad_client import get_vm_state as troshkad_get_vm_state
    from app.models.host import Host

    snapshot = {
        "type": "snapshot",
        "project_state": project.state,
        "deploy_error": project.deploy_error,
        "deploy_progress": _deploy_progress.get(project.id),
        "vm_states": {},
        "vm_progress": {},
    }

    if not project.host_id:
        return snapshot

    host = db.query(Host).filter_by(id=project.host_id).first()
    if not host or not host.ip_address:
        return snapshot

    for node in (project.topology or {}).get("nodes", []):
        if node.get("type") != "vmNode":
            continue
        dom_name = _domain_name(project.id, node["id"])
        if dom_name in _redeploy_progress:
            snapshot["vm_states"][node["id"]] = "redeploying"
            snapshot["vm_progress"][node["id"]] = _redeploy_progress[dom_name]
        else:
            state = troshkad_get_vm_state(host, dom_name)
            if state == "shut_off":
                state = "stopped"
            snapshot["vm_states"][node["id"]] = state

    return snapshot


@router.websocket("/api/v1/projects/{project_id}/ws")
async def project_websocket(websocket: WebSocket, project_id: str):
    token = websocket.query_params.get("token")

    db = SessionLocal()
    try:
        user = _authenticate_ws(token, db)
        if not user:
            await websocket.close(code=4001, reason="Unauthorized")
            return

        project = db.query(Project).filter_by(id=project_id).first()
        if not project:
            await websocket.close(code=4004, reason="Project not found")
            return
        if project.owner_id != user.id and user.role != "admin":
            await websocket.close(code=4003, reason="Access denied")
            return

        await websocket.accept()
        subscribe(project_id, websocket)

        try:
            snapshot = _build_snapshot(project, db)
            await websocket.send_json(snapshot)
        finally:
            db.close()
            db = None

        # Keep alive: listen for client messages, send heartbeat pings
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_json({"type": "ping"})
            except WebSocketDisconnect:
                break

    except Exception:
        logger.debug("WebSocket error for project %s", project_id[:8], exc_info=True)
    finally:
        unsubscribe(project_id, websocket)
        if db:
            db.close()
```

- [ ] **Step 2: Register the WS router and capture event loop in main.py**

Modify `src/backend/app/main.py`. Add these imports and changes:

After the existing `from app.services.health_poller import start_health_poller` line inside the lifespan function, add the event loop capture:

```python
@asynccontextmanager
async def lifespan(app):
    from app.services.health_poller import start_health_poller
    from app.services.ws_pubsub import set_event_loop
    import asyncio
    set_event_loop(asyncio.get_running_loop())
    start_health_poller()
    yield
```

Note: `start_state_poller()` will be added to main.py in Task 3 after the poller function is created.

After the existing router imports (after the `eip_routes` line), add:

```python
from app.api import ws as ws_routes  # noqa: E402
```

After the last `app.include_router(...)` line, add:

```python
app.include_router(ws_routes.router)
```

Note: the WS router is NOT prefixed with `/api/v1` because the route already includes the full path in the decorator.

- [ ] **Step 3: Run the existing tests to make sure nothing broke**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v --timeout=30`
Expected: All existing tests PASS

- [ ] **Step 4: Commit**

```bash
git add src/backend/app/api/ws.py src/backend/app/main.py
git commit -m "feat: add WebSocket endpoint for per-project state updates"
```

---

### Task 3: Background State Poller

**Files:**
- Modify: `src/backend/app/services/ws_pubsub.py`

The background poller runs every 5 seconds, queries troshkad for VM states on projects with active subscribers, and pushes diffs.

- [ ] **Step 1: Add the background state poller to ws_pubsub.py**

Add to the end of `src/backend/app/services/ws_pubsub.py`:

```python
_last_states: dict[str, dict] = {}
_POLL_INTERVAL = 5


def _poll_loop():
    logger.info("WS state poller started (interval=%ds)", _POLL_INTERVAL)
    import time
    while True:
        time.sleep(_POLL_INTERVAL)
        try:
            _poll_active_projects()
        except Exception:
            logger.exception("WS state poller error")


def _poll_active_projects():
    project_ids = get_active_project_ids()
    if not project_ids:
        return

    from app.core.database import SessionLocal
    from app.models.project import Project
    from app.models.host import Host
    from app.services.troshkad_client import get_vm_state as troshkad_get_vm_state
    from app.api.projects import _domain_name, _redeploy_progress
    from app.services.deploy_service import _deploy_progress

    db = SessionLocal()
    try:
        for project_id in project_ids:
            project = db.query(Project).filter_by(id=project_id).first()
            if not project:
                continue

            # Always push project state changes
            last = _last_states.get(project_id, {})
            current_project_state = project.state
            current_deploy_error = project.deploy_error
            if last.get("project_state") != current_project_state or last.get("deploy_error") != current_deploy_error:
                notify_project(project_id, {
                    "type": "project-state",
                    "state": current_project_state,
                    "deploy_error": current_deploy_error,
                })

            # Push deploy progress if active
            dp = _deploy_progress.get(project_id)
            if dp and dp != last.get("deploy_progress"):
                notify_project(project_id, {"type": "deploy-progress", "progress": dp})

            # Poll VM states only if project has a host
            vm_states = {}
            vm_progress = {}
            if project.host_id:
                host = db.query(Host).filter_by(id=project.host_id).first()
                if host and host.ip_address:
                    for node in (project.topology or {}).get("nodes", []):
                        if node.get("type") != "vmNode":
                            continue
                        dom_name = _domain_name(project.id, node["id"])
                        if dom_name in _redeploy_progress:
                            vm_states[node["id"]] = "redeploying"
                            vm_progress[node["id"]] = _redeploy_progress[dom_name]
                        else:
                            state = troshkad_get_vm_state(host, dom_name)
                            if state == "shut_off":
                                state = "stopped"
                            vm_states[node["id"]] = state

            if vm_states != last.get("vm_states") or vm_progress != last.get("vm_progress"):
                notify_project(project_id, {
                    "type": "vm-state",
                    "states": vm_states,
                    "progress": vm_progress,
                })

            _last_states[project_id] = {
                "project_state": current_project_state,
                "deploy_error": current_deploy_error,
                "deploy_progress": dp,
                "vm_states": vm_states,
                "vm_progress": vm_progress,
            }
    finally:
        db.close()


def start_state_poller():
    thread = threading.Thread(target=_poll_loop, daemon=True, name="ws-state-poller")
    thread.start()
    return thread
```

- [ ] **Step 2: Add start_state_poller to main.py lifespan**

Modify `src/backend/app/main.py` — update the lifespan to also start the state poller:

```python
@asynccontextmanager
async def lifespan(app):
    from app.services.health_poller import start_health_poller
    from app.services.ws_pubsub import set_event_loop, start_state_poller
    import asyncio
    set_event_loop(asyncio.get_running_loop())
    start_health_poller()
    start_state_poller()
    yield
```

- [ ] **Step 3: Run tests**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/test_ws_pubsub.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/backend/app/services/ws_pubsub.py src/backend/app/main.py
git commit -m "feat: add background state poller for WebSocket subscribers"
```

---

### Task 4: Backend Mutation Hooks

**Files:**
- Modify: `src/backend/app/api/projects.py`
- Modify: `src/backend/app/services/deploy_service.py`

Add `notify_project()` calls after every state-changing action for instant feedback.

- [ ] **Step 1: Add notify calls to VM power actions in projects.py**

Add import at top of `src/backend/app/api/projects.py` (after the existing `from app.services.console_proxy import get_or_create_proxy` line):

```python
from app.services.ws_pubsub import notify_project
```

**stop_vm** (line 444): After `wait_for_job` succeeds or fails, notify with the new state. Modify the function:

```python
@router.post("/{project_id}/vms/{vm_id}/stop")
def stop_vm(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    try:
        job_id = start_job(host, "/vms/stop", {"domain_name": dom})
        wait_for_job(host, job_id, timeout=60, poll_interval=2)
        notify_project(project_id, {"type": "vm-state", "states": {vm_id: "stopped"}, "progress": {}})
        return {"action": "stop", "success": True}
    except TroshkadError as e:
        logger.error("Failed to stop VM %s: %s", dom, e)
        return {"action": "stop", "success": False}
```

**forcestop_vm** (line 465): Same pattern:

```python
@router.post("/{project_id}/vms/{vm_id}/forcestop")
def forcestop_vm(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    try:
        job_id = start_job(host, "/vms/force-off", {"domain_name": dom})
        wait_for_job(host, job_id, timeout=30, poll_interval=2)
        notify_project(project_id, {"type": "vm-state", "states": {vm_id: "stopped"}, "progress": {}})
        return {"action": "forcestop", "success": True}
    except TroshkadError as e:
        logger.error("Failed to force-stop VM %s: %s", dom, e)
        return {"action": "forcestop", "success": False}
```

**restart_vm** (line 478): Notify running state after reboot:

```python
@router.post("/{project_id}/vms/{vm_id}/restart")
def restart_vm(project_id: str, vm_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    project, host = _get_project_and_host(project_id, user, db)
    dom = _domain_name(project_id, vm_id)
    try:
        job_id = start_job(host, "/vms/reboot", {"domain_name": dom})
        wait_for_job(host, job_id, timeout=60, poll_interval=2)
        notify_project(project_id, {"type": "vm-state", "states": {vm_id: "running"}, "progress": {}})
        return {"action": "restart", "success": True}
    except TroshkadError as e:
        logger.error("Failed to restart VM %s: %s", dom, e)
        return {"action": "restart", "success": False}
```

**start_vm — _cache_and_start thread** (line 419): After the `wait_for_job` call inside `_cache_and_start()`, add notify. This runs inside a background thread, so `notify_project()` will use `run_coroutine_threadsafe`:

After line 434 (`wait_for_job(h, job_id, timeout=60, poll_interval=2)`), add:

```python
                notify_project(p_id, {"type": "vm-state", "states": {vm_id: "running"}, "progress": {}})
```

**start_vm — _start_infra_then_vm thread** (line 343): After line 395 (`wait_for_job(h, job_id, timeout=60, poll_interval=2)`), add:

```python
                    notify_project(p_id, {"type": "vm-state", "states": {target_vm_id: "running"}, "progress": {}})
```

After line 399 (`proj.state = "active"` / `s.commit()`), add:

```python
                notify_project(p_id, {"type": "project-state", "state": "active", "deploy_error": None})
```

**stop_project** (line 174): After setting `project.state = "stopping"` and committing (line 189), add:

```python
    notify_project(project_id, {"type": "project-state", "state": "stopping", "deploy_error": None})
```

**start_project** (line 228): After setting `project.state = "starting"` and committing (line 243), add:

```python
    notify_project(project_id, {"type": "project-state", "state": "starting", "deploy_error": None})
```

**force_stop_project** (line 197): After setting `project.state = "stopped"` and committing (line 224), add:

```python
    notify_project(project_id, {"type": "project-state", "state": "stopped", "deploy_error": None})
```

- [ ] **Step 2: Add notify calls to deploy_service.py**

Add import at top of `src/backend/app/services/deploy_service.py` (after the existing imports):

```python
from app.services.ws_pubsub import notify_project
```

**deploy_project_async** — add notify after each `_deploy_progress` write. After each line that sets `_deploy_progress[project_id] = {...}`, add a corresponding notify call. For example:

After line 620 (`_deploy_progress[project_id] = {"step": "networking", "detail": "configuring VXLAN"}`):
```python
        notify_project(project_id, {"type": "deploy-progress", "progress": _deploy_progress[project_id]})
```

Repeat this pattern after lines 635, 688, 693, 698, 706, 715. The download progress callback `_deploy_dl_progress` (line 700) also needs a notify:

```python
        def _deploy_dl_progress(downloaded, total):
            pct = f"{int(downloaded / max(total, 1) * 100)}%" if total > 0 else "..."
            _deploy_progress[project_id] = {"step": "downloading images", "detail": pct}
            notify_project(project_id, {"type": "deploy-progress", "progress": _deploy_progress[project_id]})
```

After deploy completes successfully (line 722, after `project.state = "active"`):
```python
        notify_project(project_id, {"type": "project-state", "state": "active", "deploy_error": None})
```

After deploy fails (line 733, after `project.state = "error"`):
```python
            notify_project(project_id, {"type": "project-state", "state": "error", "deploy_error": project.deploy_error})
```

**stop_project_async** — after line 785 (`project.state = "stopped"` / `s.commit()`):
```python
        notify_project(project_id, {"type": "project-state", "state": "stopped", "deploy_error": None})
```

After error (line 796):
```python
                notify_project(project_id, {"type": "project-state", "state": "error", "deploy_error": project.deploy_error})
```

**start_project_async** — same pattern at the end of the function where project.state is set.

- [ ] **Step 3: Run backend tests**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v --timeout=30`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add src/backend/app/api/projects.py src/backend/app/services/deploy_service.py
git commit -m "feat: add WebSocket notify hooks to all state-changing actions"
```

---

### Task 5: Frontend useVmStateSocket Hook

**Files:**
- Create: `src/frontend/src/hooks/useVmStateSocket.ts`

- [ ] **Step 1: Create the hook**

Create `src/frontend/src/hooks/useVmStateSocket.ts`:

```typescript
"use client";

import { useState, useEffect, useRef, useCallback } from "react";

interface VmProgress {
  step: string;
  detail: string;
}

interface DeployProgress {
  step: string;
  detail: string;
}

interface VmStateSocket {
  connected: boolean;
  vmStates: Record<string, string>;
  vmProgress: Record<string, VmProgress>;
  projectState: string | null;
  deployError: string | null;
  deployProgress: DeployProgress | null;
}

const BACKOFF_BASE = 1000;
const BACKOFF_MAX = 10000;

export function useVmStateSocket(projectId: string | null): VmStateSocket {
  const [connected, setConnected] = useState(false);
  const [vmStates, setVmStates] = useState<Record<string, string>>({});
  const [vmProgress, setVmProgress] = useState<Record<string, VmProgress>>({});
  const [projectState, setProjectState] = useState<string | null>(null);
  const [deployError, setDeployError] = useState<string | null>(null);
  const [deployProgress, setDeployProgress] = useState<DeployProgress | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const retriesRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);

  const connect = useCallback(() => {
    if (!projectId || !mountedRef.current) return;

    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const host = window.location.host;
    const url = `${proto}//${host}/api/v1/projects/${projectId}/ws`;

    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      if (!mountedRef.current) { ws.close(); return; }
      setConnected(true);
      retriesRef.current = 0;
    };

    ws.onmessage = (e) => {
      if (!mountedRef.current) return;
      try {
        const msg = JSON.parse(e.data);
        switch (msg.type) {
          case "snapshot":
            setVmStates(msg.vm_states || {});
            setVmProgress(msg.vm_progress || {});
            setProjectState(msg.project_state || null);
            setDeployError(msg.deploy_error || null);
            setDeployProgress(msg.deploy_progress || null);
            break;
          case "vm-state":
            setVmStates(msg.states || {});
            setVmProgress(msg.progress || {});
            break;
          case "project-state":
            setProjectState(msg.state || null);
            setDeployError(msg.deploy_error ?? null);
            break;
          case "deploy-progress":
            setDeployProgress(msg.progress || null);
            break;
          case "ping":
            break;
        }
      } catch { /* ignore malformed messages */ }
    };

    ws.onclose = () => {
      if (!mountedRef.current) return;
      setConnected(false);
      wsRef.current = null;
      const delay = Math.min(BACKOFF_BASE * Math.pow(2, retriesRef.current), BACKOFF_MAX);
      retriesRef.current += 1;
      timerRef.current = setTimeout(connect, delay);
    };

    ws.onerror = () => {
      // onclose will fire after onerror — reconnect handled there
    };
  }, [projectId]);

  useEffect(() => {
    mountedRef.current = true;
    connect();

    return () => {
      mountedRef.current = false;
      if (timerRef.current) clearTimeout(timerRef.current);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [connect]);

  return { connected, vmStates, vmProgress, projectState, deployError, deployProgress };
}
```

- [ ] **Step 2: Verify the file compiles**

Run: `cd src/frontend && npx tsc --noEmit src/hooks/useVmStateSocket.ts 2>&1 | head -20`

If there are import path issues, check `tsconfig.json` for path aliases.

- [ ] **Step 3: Commit**

```bash
git add src/frontend/src/hooks/useVmStateSocket.ts
git commit -m "feat: add useVmStateSocket hook for real-time state updates"
```

---

### Task 6: Integrate Hook into Canvas Page

**Files:**
- Modify: `src/frontend/src/app/projects/[id]/page.tsx`

Replace the three polling loops (project state, deploy progress, VM states) and the localStorage listener with the WebSocket hook.

- [ ] **Step 1: Add the hook import and call**

At the top of `src/frontend/src/app/projects/[id]/page.tsx`, add the import:

```typescript
import { useVmStateSocket } from "@/hooks/useVmStateSocket";
```

Inside `ProjectCanvasPage()`, after the existing state declarations (after line 28 `const [projectState, setProjectState] = useState("draft");`), add:

```typescript
  const ws = useVmStateSocket(projectId);
```

- [ ] **Step 2: Replace project state polling with WebSocket**

**Remove** the `fetchProjectState` function (lines 48-64) and its initial call `useEffect` (lines 66-68), and the transitional state polling `useEffect` (lines 72-78).

**Replace** with a `useEffect` that watches the WebSocket's `projectState`:

```typescript
  useEffect(() => {
    if (!ws.projectState) return;
    const wasTransitional = ["reconfiguring", "deploying", "starting"].includes(prevStateRef.current);
    setProjectState(ws.projectState);
    setDeployError(ws.deployError || null);
    prevStateRef.current = ws.projectState;
    if (wasTransitional && ws.projectState === "active") {
      loadProject(projectId);
    }
  }, [ws.projectState, ws.deployError]);
```

Also keep the initial REST fetch for `projectName` since the WebSocket doesn't carry the name. Add a one-time fetch:

```typescript
  useEffect(() => {
    fetch(`/api/v1/projects/${projectId}`)
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (data) {
          setProjectName(data.name);
          setProjectState(data.state);
          setDeployError(data.deploy_error || null);
          prevStateRef.current = data.state;
        }
      })
      .catch(() => {});
  }, [projectId]);
```

- [ ] **Step 3: Replace deploy progress polling with WebSocket**

**Remove** the deploy progress polling `useEffect` (lines 80-92).

**Replace** with:

```typescript
  useEffect(() => {
    setDeployProgress(ws.deployProgress);
  }, [ws.deployProgress]);
```

- [ ] **Step 4: Replace VM state sync with WebSocket**

**Remove** the `syncVmStates` function (lines 105-153), its initial-call `useEffect` (lines 155-157), the redeploy polling `useEffect` (lines 159-166), and the localStorage listener `useEffect` (lines 168-174).

**Replace** with a `useEffect` that watches `ws.vmStates` and updates the canvas store:

```typescript
  useEffect(() => {
    if (!Object.keys(ws.vmStates).length) return;

    const ids = new Set<string>(Object.keys(ws.vmStates));
    const hasUndeployed = Object.values(ws.vmStates).some((s) => s === "not_found");
    setDeployedVmIds(ids);
    useCanvasStore.setState({ deployedVmIds: ids });
    if (hasUndeployed) {
      useCanvasStore.setState({ topologyDirty: true });
    }

    const store = useCanvasStore.getState();
    useCanvasStore.setState({
      nodes: store.nodes.map((node) => {
        if (node.type !== "vmNode") return node;
        if (node.id in ws.vmStates) {
          const redeployInfo = ws.vmProgress[node.id];
          return { ...node, data: { ...node.data, status: ws.vmStates[node.id], redeployStep: redeployInfo?.step || null, redeployDetail: redeployInfo?.detail || null } };
        }
        return { ...node, data: { ...node.data, status: "stopped", redeployStep: null, redeployDetail: null } };
      }),
    });
  }, [ws.vmStates, ws.vmProgress]);
```

Note: The dirty flag check that fetches deployed_topology should remain as a one-time check after project load — add it to the existing `projectId` useEffect:

```typescript
  useEffect(() => {
    fetch(`/api/v1/projects/${projectId}`)
      .then((r) => r.ok ? r.json() : null)
      .then((data) => {
        if (!data) return;
        setProjectName(data.name);
        setProjectState(data.state);
        setDeployError(data.deploy_error || null);
        prevStateRef.current = data.state;
        const currentNodes = (data.topology?.nodes || []).map((n: Record<string, unknown>) => n.id).sort();
        const deployedNodes = (data.deployed_topology?.nodes || []).map((n: Record<string, unknown>) => n.id).sort();
        if (JSON.stringify(currentNodes) !== JSON.stringify(deployedNodes)) {
          useCanvasStore.setState({ topologyDirty: true });
        }
        const depSizes: Record<string, number> = {};
        for (const n of (data.deployed_topology?.nodes || [])) {
          if (n.type === "storageNode" && n.data?.size) {
            depSizes[n.id] = n.data.size;
          }
        }
        useCanvasStore.setState({ deployedDiskSizes: depSizes });
      })
      .catch(() => {});
  }, [projectId]);
```

- [ ] **Step 5: Verify frontend compiles**

Run: `cd src/frontend && npx next build 2>&1 | tail -20`

Or just check TypeScript: `cd src/frontend && npx tsc --noEmit 2>&1 | head -30`

- [ ] **Step 6: Commit**

```bash
git add src/frontend/src/app/projects/\\[id\\]/page.tsx
git commit -m "feat: replace canvas page polling with WebSocket hook"
```

---

### Task 7: Integrate Hook into Console Page

**Files:**
- Modify: `src/frontend/src/app/console/page.tsx`

Replace the VM state polling and localStorage writes with the WebSocket hook.

- [ ] **Step 1: Add the hook import and call**

At the top of `src/frontend/src/app/console/page.tsx`, add:

```typescript
import { useVmStateSocket } from "@/hooks/useVmStateSocket";
```

Inside `ConsolePage()`, after the existing state declarations (after line 31 `const mountedRef = useRef(true);`), add:

```typescript
  const ws = useVmStateSocket(projectId);
```

- [ ] **Step 2: Replace fetchVmState polling with WebSocket**

**Remove** the `fetchVmState` callback (lines 178-184) and its polling `useEffect` (lines 186-190).

**Replace** with a `useEffect` that derives single VM state from the hook:

```typescript
  useEffect(() => {
    if (!vmId || !ws.vmStates[vmId]) return;
    setVmState(ws.vmStates[vmId]);
  }, [ws.vmStates, vmId]);
```

- [ ] **Step 3: Simplify vmPowerAction — remove localStorage and setTimeout hack**

**Replace** the `vmPowerAction` callback (lines 249-265) with:

```typescript
  const vmPowerAction = useCallback(async (action: string, label: string, confirm?: string) => {
    if (confirm && !window.confirm(confirm)) return;
    if (action === "start") { startingRef.current = true; setStatus("Starting..."); }
    try {
      const resp = await fetch(`/api/v1/projects/${projectId}/vms/${vmId}/${action}`, { method: "POST" });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: `${label} failed` }));
        alert(err.detail || `${label} failed`);
      }
    } catch {
      alert("Failed to connect to server");
    }
  }, [projectId, vmId]);
```

The `setTimeout`, `fetchVmState()`, and `localStorage.setItem("troshka-vm-power", ...)` are all removed. The WebSocket will push the new state after the backend action completes.

- [ ] **Step 4: Remove fetchVmState from the dependency array of vmPowerAction**

Since `fetchVmState` is removed, `vmPowerAction` no longer needs it in its deps. The deps are now just `[projectId, vmId]`.

- [ ] **Step 5: Verify frontend compiles**

Run: `cd src/frontend && npx tsc --noEmit 2>&1 | head -30`

- [ ] **Step 6: Commit**

```bash
git add src/frontend/src/app/console/page.tsx
git commit -m "feat: replace console page polling with WebSocket hook"
```

---

### Task 8: Manual Integration Test

**Files:** None (testing only)

- [ ] **Step 1: Start the dev environment**

Ask the user if you should restart the backend via `./dev-services.sh restart backend`. Frontend hot-reloads automatically.

- [ ] **Step 2: Verify WebSocket connects**

Open the browser to a deployed project canvas page. Open browser DevTools → Network → WS tab. Confirm:
- A WebSocket connection to `/api/v1/projects/{id}/ws` is established
- A `snapshot` message arrives with `vm_states`, `project_state`, etc.
- `ping` messages arrive every 30 seconds

- [ ] **Step 3: Test cross-window sync**

Open the same project's VM console in a new window. From the console, stop a running VM. Verify:
- Console power menu updates immediately (VM shows as stopped)
- Canvas page VM node updates within 1-2 seconds (no 5-second delay)
- No localStorage writes in DevTools → Application → Local Storage

- [ ] **Step 4: Test reconnection**

Restart the backend (`./dev-services.sh restart backend`). Verify:
- WebSocket reconnects after a brief delay
- Full state snapshot is received on reconnect
- VM states are correct after reconnect

- [ ] **Step 5: Test deploy progress**

Deploy a new project. Verify:
- Deploy progress messages flow through the WebSocket (visible in Network → WS)
- Canvas toolbar shows progress updates in real-time
- Project state transitions from `deploying` → `active` via WebSocket

- [ ] **Step 6: Commit any fixes**

If any issues were found and fixed during testing:

```bash
git add -A
git commit -m "fix: address issues found during WebSocket integration testing"
```
