# Project Auto-Stop & Auto-Delete Timers

## Overview

Add two independent, user-configurable timers to projects:

- **Auto-stop**: stops all VMs after a configured duration of runtime. Resets each time the project is started.
- **Auto-delete**: destroys and deletes the project after a configured duration from first deploy. Fires regardless of project state.

Default for both: no limit (disabled). Settable from the canvas PROJECT palette section and via the REST API. Preset durations with a custom hours+minutes option.

## Data Model

### New columns on `projects` table

| Column | Type | Nullable | Description |
|--------|------|----------|-------------|
| `auto_stop_minutes` | Integer | Yes | Configured auto-stop duration in minutes |
| `auto_stop_started_at` | DateTime(tz) | Yes | When the stop countdown began (set on deploy/start completion) |
| `auto_stop_expires_at` | DateTime(tz) | Yes | Absolute expiry: `started_at + timedelta(minutes=auto_stop_minutes)` |
| `auto_stop_warned` | Boolean | No (default False) | Whether the 5-minute warning was sent |
| `auto_delete_minutes` | Integer | Yes | Configured auto-delete duration in minutes |
| `auto_delete_started_at` | DateTime(tz) | Yes | When the delete countdown began (set on first deploy) |
| `auto_delete_warned` | Boolean | No (default False) | Whether the 5-minute warning was sent |

### Columns to drop

| Column | Reason |
|--------|--------|
| `run_timer_hours` | Replaced by `auto_stop_minutes` |
| `run_timer_max_ext_hours` | Not needed — users can extend freely |
| `run_timer_started_at` | Replaced by `auto_stop_started_at` |

### Reused column

- `lifetime_expires_at` — becomes the computed auto-delete expiry: `auto_delete_started_at + timedelta(minutes=auto_delete_minutes)`

### Timer lifecycle

**Auto-stop:**
- `auto_stop_started_at` set when project enters `active` (deploy completes or start completes)
- `auto_stop_expires_at` computed as `auto_stop_started_at + timedelta(minutes=auto_stop_minutes)`
- Both cleared when project stops (timer is consumed; will restart on next start)
- `auto_stop_warned` reset to False on each start

**Auto-delete:**
- `auto_delete_started_at` set on first deploy only (never cleared afterward)
- `lifetime_expires_at` computed as `auto_delete_started_at + timedelta(minutes=auto_delete_minutes)`
- Keeps ticking through stop/start cycles — never paused
- `auto_delete_warned` reset to False when timer is extended

**User changes duration while timer is running:**
- Recompute `*_expires_at` from the original `*_started_at` + new duration
- Reset `*_warned` to False

**User disables timer (sets to null):**
- Clear `*_expires_at`, `*_started_at`, `*_warned`

## Backend Timer Service

### New file: `src/backend/app/services/project_timer.py`

Daemon thread started from `main.py` alongside `start_health_poller()`.

```
start_project_timer()
  → spawns daemon thread running _timer_loop()

_timer_loop():
  every 30 seconds:
    with SessionLocal():

      1. EXPIRED AUTO-STOP
         Query: auto_stop_expires_at <= now() AND state = 'active'
         Action: set state='stopping', spawn stop_project_async() thread
         Clear: auto_stop_started_at, auto_stop_expires_at

      2. EXPIRED AUTO-DELETE
         Query: lifetime_expires_at <= now() AND state IN ('active','stopped','error','draft')
         Action:
           - If active → stop first (wait for completion), then delete
           - If stopped/error/draft → delete directly
         Delete: destroy_project_sync() + db.delete(project) + db.commit()

      3. AUTO-STOP WARNING (5 min)
         Query: auto_stop_expires_at - now() <= 5 min AND auto_stop_warned = False AND state = 'active'
         Action: send WebSocket timer_warning, set auto_stop_warned = True

      4. AUTO-DELETE WARNING (5 min)
         Query: lifetime_expires_at - now() <= 5 min AND auto_delete_warned = False
         Action: send WebSocket timer_warning, set auto_delete_warned = True

      5. Skip projects in transitional states: deploying, stopping, starting, reconfiguring, migrating
```

### Startup recovery

On backend start, the timer service's first loop iteration catches any timers that expired while the backend was down and fires them immediately.

### Error handling

- If stop fails during auto-delete (host unreachable), project goes to `error` state. Delete still proceeds on next cycle since `error` is an eligible state.
- Each action gets a fresh `SessionLocal()` — standard background thread pattern.
- Log all timer firings at INFO level: `"Auto-stop fired for project {name} ({id[:8]})"`.

## API Changes

### PATCH `/projects/{project_id}` — extended fields

Accepts:
```json
{
  "auto_stop_minutes": 120,
  "auto_delete_minutes": 480
}
```

- Setting a value computes `*_expires_at` if the timer is currently running (project is active for auto-stop, or has been deployed for auto-delete).
- Setting to `null` disables the timer and clears all related fields.
- Resets `*_warned` to False when value changes.

### GET `/projects/{project_id}` — extended response

Adds to response:
```json
{
  "auto_stop_minutes": 120,
  "auto_stop_expires_at": "2026-06-17T14:30:00Z",
  "auto_delete_minutes": 480,
  "lifetime_expires_at": "2026-06-17T20:00:00Z"
}
```

### POST `/projects/{project_id}/extend-timer` — new endpoint

```json
{
  "timer": "auto_stop" | "auto_delete",
  "add_minutes": 60
}
```

- Pushes `*_expires_at` forward by `add_minutes`.
- Resets `*_warned` to False.
- Returns updated project with new expiry times.
- Returns 400 if the specified timer is not active.

### Timer reset on state transitions

| Transition | Auto-stop effect | Auto-delete effect |
|------------|-----------------|-------------------|
| Deploy completes → active | Set `started_at=now()`, compute `expires_at` | If first deploy: set `started_at=now()`, compute `expires_at` |
| Start completes → active | Set `started_at=now()`, compute `expires_at` | No change (keeps ticking) |
| Stop completes → stopped | Clear `started_at`, `expires_at` | No change (keeps ticking) |
| Auto-stop fires | Clear `started_at`, `expires_at` | No change |
| Auto-delete fires | N/A (project deleted) | N/A |

## WebSocket Messages

### Timer warning (5 minutes before expiry)

```json
{
  "type": "timer_warning",
  "timer": "auto_stop" | "auto_delete",
  "expires_at": "2026-06-17T14:30:00Z",
  "minutes_remaining": 5
}
```

### Timer fired

```json
{
  "type": "timer_fired",
  "timer": "auto_stop" | "auto_delete"
}
```

Sent through the existing project WebSocket channel (`ConnectionManager`). The `useVmStateSocket` hook receives these and dispatches to UI handlers.

## Frontend UI

### PROJECT palette section (Palette.tsx)

Two new items below Start Order:

```
PROJECT ▾
  🔢 Start Order
     VM boot sequence
  ⏱ Auto-Stop              [None ▾]
     Stop VMs after duration
  🗑 Auto-Delete            [None ▾]
     Delete project after duration
```

Each has a dropdown on the right side with presets:
- `None`, `30m`, `1h`, `2h`, `4h`, `8h`, `24h`, `Custom...`

Selecting **Custom** expands an inline row with two number inputs and a Set button:
```
  [__] h  [__] m  [Set]
```

Changing a preset or clicking Set immediately PATCHes `/projects/{id}` with the new `auto_stop_minutes` or `auto_delete_minutes` value.

Only shown to project owner. Portal/guest users cannot see or modify timers.

### Action bar countdown (page.tsx)

When either timer is active, a badge appears next to the project state pill:

```
myproject  [active]  ⏱ 1h 42m
```

Color escalation:
- **> 15 min**: subtle/gray text
- **5–15 min**: yellow text
- **< 5 min**: red text, pulsing animation

If both timers are active, show whichever expires sooner. Clicking the badge opens the PROJECT palette section.

Countdown computed client-side from `*_expires_at` with a 1-second `setInterval`. No polling needed.

### Toast notification

When WebSocket `timer_warning` arrives, a PatternFly-style alert slides in from the top:

```
⚠ Auto-stop in 5 minutes    [Extend 1h] [Dismiss]
```

- **Extend 1h** calls `POST /extend-timer` with `add_minutes: 60`
- **Dismiss** closes the toast
- Auto-delete warning uses the same pattern:
```
⚠ Auto-delete in 5 minutes  [Extend 1h] [Dismiss]
```

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| Backend restarts with expired timers | First loop iteration catches and fires them immediately |
| Project in transitional state at expiry | Skipped; picked up next cycle when state settles |
| User disables timer while warning toast is showing | Toast dismiss/extend is harmless (backend no-ops or returns 400) |
| Auto-delete of active project | Stop first (wait), then delete |
| Auto-delete of stopped/error/draft project | Delete directly |
| Multiple browser tabs | Countdown computed client-side (all tabs agree). Toast fires on each tab independently (acceptable). |
| Host unreachable during auto-stop/delete | Project goes to error state; auto-delete retries next cycle |

## Migration

Single Alembic migration:
1. Add `auto_stop_minutes`, `auto_stop_started_at`, `auto_stop_expires_at`, `auto_stop_warned`, `auto_delete_minutes`, `auto_delete_started_at`, `auto_delete_warned`
2. Drop `run_timer_hours`, `run_timer_max_ext_hours`, `run_timer_started_at`
3. Keep `lifetime_expires_at` (reused for auto-delete)

## Files to create/modify

### New files
- `src/backend/app/services/project_timer.py` — timer daemon service

### Modified files
- `src/backend/app/models/project.py` — new columns, drop old ones
- `src/backend/app/api/projects.py` — extend PATCH/GET, add extend-timer endpoint, set timers on state transitions
- `src/backend/app/main.py` — start timer service
- `src/backend/alembic/versions/xxxx_add_project_timers.py` — migration
- `src/frontend/src/components/canvas/Palette.tsx` — timer controls in PROJECT section
- `src/frontend/src/app/projects/[id]/page.tsx` — action bar countdown badge, toast notifications
- `src/frontend/src/hooks/useVmStateSocket.ts` — handle timer_warning and timer_fired messages
