# WebSocket VM State Sync — Design Spec

## Problem

When a user controls a VM from the console window (start, stop, force off), the canvas in the project window doesn't update until its next poll cycle. Cross-tab messaging via `localStorage("troshka-vm-power")` is unreliable across separate browser windows. The canvas only polls VM states during redeploy — there is no periodic poll for running projects, so power state changes from the console are invisible until page reload.

Additionally, deploy progress and project state transitions are polled at 3-second intervals, adding unnecessary load and latency.

## Goal

Real-time VM state, project state, and deploy progress updates across all open windows/tabs for a project, with sub-second latency on user-initiated actions and ~5-second detection of external state changes.

## Architecture

### Message Types

All WebSocket messages are JSON objects with a `"type"` field:

```json
{"type": "vm-state", "states": {"vm-id-1": "running", "vm-id-2": "stopped"}, "progress": {"vm-id-3": {"step": "creating disk", "detail": "50%"}}}
```

```json
{"type": "project-state", "state": "active", "deploy_error": null}
```

```json
{"type": "deploy-progress", "progress": {"step": "downloading images", "detail": "45%"}}
```

On initial connect and reconnect, the server sends a snapshot message containing all three:

```json
{"type": "snapshot", "vm_states": {...}, "vm_progress": {...}, "project_state": "active", "deploy_error": null, "deploy_progress": null}
```

### Backend: Pub/Sub Module

**New file: `src/backend/app/services/ws_pubsub.py`**

In-memory pub/sub following the existing `_active_proxies` pattern from `console_proxy.py`:

```python
_subscribers: dict[str, set[WebSocket]] = {}   # project_id → connected clients
_lock = threading.Lock()
```

Functions:

- `subscribe(project_id, ws)` — add client to project's subscriber set
- `unsubscribe(project_id, ws)` — remove client, clean up empty sets
- `notify_project(project_id, message: dict)` — serialize to JSON, copy subscriber set under lock, release lock, iterate copy to send, remove dead connections on failure
- `get_active_project_ids() → set[str]` — returns project IDs with connected clients (for background poller)

Thread safety: `notify_project()` acquires lock, copies the set, releases lock, then sends. Dead connections detected via send exception, removed in a second lock acquisition. No lock held during I/O.

Async/sync bridge: FastAPI WebSocket handlers are async, but mutation hooks fire from sync background threads (deployer, power actions). The pub/sub module stores an `asyncio.AbstractEventLoop` reference (captured at startup). Sync callers use `asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)` to push messages from threads into the async event loop. The background state poller runs as a daemon thread and uses the same bridge.

### Backend: Background State Poller

A daemon thread started at app startup (like `health_poller`):

- Every 5 seconds, for each project with active WebSocket subscribers:
  - Fetch all VM states from troshkad via existing `troshkad_get_vm_state()`
  - Compare to last-known cached state
  - If changed, call `notify_project()` with `vm-state` message
- Only queries projects with connected clients
- Catches external state changes (VM crash, virsh operations, host-level changes)
- Caches last-sent state per project to avoid pushing duplicate snapshots

### Backend: WebSocket Endpoint

**In `src/backend/app/api/projects.py`:**

`WS /api/v1/projects/{project_id}/ws?token=...`

- Validates token via query param (skipped in dev mode, matching existing `get_current_user` auto-auth)
- Verifies user has access to the project (ownership check)
- Calls `subscribe(project_id, ws)`
- Sends initial snapshot: current VM states (from troshkad), project state (from DB), deploy progress (from `_deploy_progress` dict)
- Enters receive loop — listens for client pings, sends server heartbeat ping every 30s
- On disconnect: `unsubscribe(project_id, ws)`

### Backend: Mutation Hooks

After each state-changing action, call `notify_project()`:

| Location | Trigger | Message Type |
|----------|---------|--------------|
| `projects.py` — start endpoint | After troshkad start job completes (in background thread) | `vm-state` |
| `projects.py` — stop/forcestop/restart | After troshkad job completes (synchronous) | `vm-state` |
| `deploy_service.py` — progress updates | Each `_deploy_progress[project_id] = {...}` write | `deploy-progress` |
| `deploy_service.py` — deploy complete/error | After setting project state in DB | `project-state` + `vm-state` |
| `projects.py` — redeploy progress | After `_redeploy_progress` updates | `vm-state` (with progress) |
| `projects.py` — stop/start project endpoints | After project state transition | `project-state` |

### Frontend: `useVmStateSocket` Hook

**New file: `src/frontend/src/hooks/useVmStateSocket.ts`**

```typescript
function useVmStateSocket(projectId: string | null): {
  connected: boolean;
  vmStates: Record<string, string>;
  vmProgress: Record<string, { step: string; detail: string }>;
  projectState: string | null;
  deployProgress: { step: string; detail: string } | null;
}
```

Behavior:

- Opens WebSocket to `/api/v1/projects/{projectId}/ws` (constructs URL from `window.location` — `ws:` for HTTP, `wss:` for HTTPS, same host)
- On `snapshot` message: sets all state fields
- On `vm-state` message: updates `vmStates` and `vmProgress`
- On `project-state` message: updates `projectState`
- On `deploy-progress` message: updates `deployProgress`
- Reconnect with exponential backoff: 1s → 2s → 4s → max 10s
- On reconnect: server sends full snapshot, so client state resets cleanly
- Closes WebSocket on unmount or when `projectId` changes
- Returns `connected` boolean for optional UI indicator

### Frontend: Canvas Page Integration

**Modified: `src/frontend/src/app/projects/[id]/page.tsx`**

- Calls `useVmStateSocket(projectId)`
- `useEffect` watches `vmStates` — updates canvas store nodes via `useCanvasStore.setState()` (same mapping logic as current `syncVmStates`)
- `useEffect` watches `projectState` from hook — updates local `projectState`, triggers `loadProject()` on deploy→active transition
- `useEffect` watches `deployProgress` — replaces `setInterval` poll

**Removed:**
- `syncVmStates` polling `setInterval` (keep function for initial load if needed as fallback)
- `fetchProjectState` polling `setInterval`
- Deploy progress polling `setInterval`
- `localStorage("troshka-vm-power")` listener (`window.addEventListener("storage", ...)`)

### Frontend: Console Page Integration

**Modified: `src/frontend/src/app/console/page.tsx`**

- Calls `useVmStateSocket(projectId)`
- Derives single VM state: `vmStates[vmId]` replaces `vmState` local state
- Power action handlers: POST to API only, no `setTimeout` + `fetchVmState` + localStorage write — WebSocket pushes new state
- `fetchVmState` kept as one-time call if needed for initial render before WS connects

**Removed:**
- `fetchVmState` `setInterval` (5-second polling loop)
- `localStorage.setItem("troshka-vm-power", ...)` after power actions
- `setTimeout(() => { fetchVmState(); ... }, 1500)` delay hack

### What Stays Unchanged

- REST endpoints (`/vm-states`, `/vms/{id}/status`, `/deploy-progress`) remain as fallback and for pages that don't need real-time (project list page, admin pages)
- VNC console WebSocket (completely separate concern — noVNC/websockify)
- Pattern/library/host polling (different resources, not in scope)
- Canvas store shape and `updateNodeData`/`setAllVmStatus` methods

## Error Handling & Edge Cases

- **Client navigates away**: `useEffect` cleanup closes WS → server `unsubscribe()` removes from set
- **Backend restarts**: all WS connections drop → clients reconnect with backoff → server sends fresh snapshot
- **Network blip**: client `onclose` fires → reconnect with backoff → snapshot on reconnect restores full state
- **Multiple tabs same project**: each gets its own WS connection, each receives same broadcasts — no dedup needed since they're independent React trees
- **Stale state**: background poller caches last-sent state, only pushes on diff. Mutation hooks always push. If both fire simultaneously, client gets two messages with same data — idempotent since it's a full state snapshot, not a delta.
- **No message queuing**: WebSocket is best-effort; reconnect + snapshot handles gaps
- **No sequence numbers**: full snapshots make ordering irrelevant

## Authentication

- Token passed as query parameter: `?token=...`
- Dev mode: token validation skipped (matching existing `get_current_user` auto-auth)
- Production/OCP: HAProxy handles WebSocket upgrade transparently; query param passes through ingress like any HTTP parameter

## What We're NOT Doing

- No Redis pub/sub (single backend process)
- No per-message sequence numbers (full snapshots)
- No guaranteed delivery / message queue (reconnect + snapshot is sufficient)
- No auth token refresh over WS (reconnect with fresh token)
- No delta/diff messages (project state is small enough for full snapshots)

## Files Changed

| File | Change |
|------|--------|
| `src/backend/app/services/ws_pubsub.py` | **New** — pub/sub module + background state poller |
| `src/backend/app/api/projects.py` | **Modified** — add WS endpoint, add `notify_project()` calls after power actions |
| `src/backend/app/services/deploy_service.py` | **Modified** — add `notify_project()` calls on progress updates and completion |
| `src/backend/app/main.py` | **Modified** — start background state poller in lifespan |
| `src/frontend/src/hooks/useVmStateSocket.ts` | **New** — shared WebSocket hook |
| `src/frontend/src/app/projects/[id]/page.tsx` | **Modified** — use hook, remove 3 polling loops + localStorage listener |
| `src/frontend/src/app/console/page.tsx` | **Modified** — use hook, remove polling + localStorage writes |
