# Project Auto-Stop & Auto-Delete Timers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two independent, user-configurable timers (auto-stop and auto-delete) to projects, with UI controls in the canvas palette, countdown badges, toast warnings, and a backend daemon that enforces expiry.

**Architecture:** New columns on the Project model replace the unused `run_timer_*` fields. A new `project_timer.py` daemon thread polls every 30s for expired timers and fires stop/delete actions. The frontend shows timer controls in the PROJECT palette section and a countdown badge in the action bar, with WebSocket-driven toast warnings at 5 minutes before expiry.

**Tech Stack:** Python 3.11 (FastAPI, SQLAlchemy 2, Alembic), Next.js 15 (React, Zustand), WebSocket (existing `ws_pubsub.py`)

## Global Constraints

- Python: use `Mapped[type]` + `mapped_column()` syntax for model columns
- UUIDs as strings: `UUID(as_uuid=False)`
- Background threads: fresh `SessionLocal()` per thread, never share DB sessions
- Frontend: `"use client"` directive, raw `fetch()`, `useState` + `useEffect`
- Tests: SQLite with type compiler overrides, `TestClient` pattern
- No auto-reload on backend — restart required after Python changes
- Run `black` before committing
- Git: always use absolute paths or cd to project root first

---

### Task 1: Database Migration & Model Update

**Files:**
- Modify: `src/backend/app/models/project.py` (lines 26-33)
- Modify: `src/backend/app/schemas/project.py` (all three schemas)
- Create: `src/backend/alembic/versions/xxxx_add_project_auto_timers.py`
- Test: `src/backend/tests/test_projects.py`

**Interfaces:**
- Produces: `Project.auto_stop_minutes`, `Project.auto_stop_started_at`, `Project.auto_stop_expires_at`, `Project.auto_stop_warned`, `Project.auto_delete_minutes`, `Project.auto_delete_started_at`, `Project.auto_delete_warned`, `Project.lifetime_expires_at` (reused). `ProjectUpdate` accepts `auto_stop_minutes: int | None` and `auto_delete_minutes: int | None`. `ProjectResponse` exposes `auto_stop_minutes`, `auto_stop_expires_at`, `auto_delete_minutes`, `lifetime_expires_at`.

- [ ] **Step 1: Write the failing test**

Add to `src/backend/tests/test_projects.py`:

```python
def test_set_auto_stop_timer():
    list_resp = client.get("/api/v1/projects", headers=HEADERS)
    project_id = list_resp.json()[0]["id"]
    resp = client.patch(
        f"/api/v1/projects/{project_id}",
        json={"auto_stop_minutes": 120},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["auto_stop_minutes"] == 120


def test_set_auto_delete_timer():
    list_resp = client.get("/api/v1/projects", headers=HEADERS)
    project_id = list_resp.json()[0]["id"]
    resp = client.patch(
        f"/api/v1/projects/{project_id}",
        json={"auto_delete_minutes": 480},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["auto_delete_minutes"] == 480


def test_disable_auto_stop_timer():
    list_resp = client.get("/api/v1/projects", headers=HEADERS)
    project_id = list_resp.json()[0]["id"]
    client.patch(
        f"/api/v1/projects/{project_id}",
        json={"auto_stop_minutes": 60},
        headers=HEADERS,
    )
    resp = client.patch(
        f"/api/v1/projects/{project_id}",
        json={"auto_stop_minutes": None},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["auto_stop_minutes"] is None
    assert resp.json().get("auto_stop_expires_at") is None


def test_get_project_includes_timer_fields():
    list_resp = client.get("/api/v1/projects", headers=HEADERS)
    project_id = list_resp.json()[0]["id"]
    resp = client.get(f"/api/v1/projects/{project_id}", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert "auto_stop_minutes" in data
    assert "auto_stop_expires_at" in data
    assert "auto_delete_minutes" in data
    assert "lifetime_expires_at" in data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_projects.py::test_set_auto_stop_timer -v`
Expected: FAIL — `auto_stop_minutes` not a valid field on ProjectUpdate

- [ ] **Step 3: Create the Alembic migration**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m alembic revision -m "add project auto timers"`

Edit the generated migration file:

```python
"""add project auto timers

Revision ID: <auto>
"""
from alembic import op
import sqlalchemy as sa


def upgrade():
    op.add_column("projects", sa.Column("auto_stop_minutes", sa.Integer(), nullable=True))
    op.add_column("projects", sa.Column("auto_stop_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("projects", sa.Column("auto_stop_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("projects", sa.Column("auto_stop_warned", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("projects", sa.Column("auto_delete_minutes", sa.Integer(), nullable=True))
    op.add_column("projects", sa.Column("auto_delete_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("projects", sa.Column("auto_delete_warned", sa.Boolean(), nullable=False, server_default="false"))
    op.drop_column("projects", "run_timer_hours")
    op.drop_column("projects", "run_timer_max_ext_hours")
    op.drop_column("projects", "run_timer_started_at")


def downgrade():
    op.add_column("projects", sa.Column("run_timer_hours", sa.Integer(), nullable=True))
    op.add_column("projects", sa.Column("run_timer_max_ext_hours", sa.Integer(), nullable=True))
    op.add_column("projects", sa.Column("run_timer_started_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_column("projects", "auto_stop_minutes")
    op.drop_column("projects", "auto_stop_started_at")
    op.drop_column("projects", "auto_stop_expires_at")
    op.drop_column("projects", "auto_stop_warned")
    op.drop_column("projects", "auto_delete_minutes")
    op.drop_column("projects", "auto_delete_started_at")
    op.drop_column("projects", "auto_delete_warned")
```

- [ ] **Step 4: Update the Project model**

In `src/backend/app/models/project.py`, replace lines 26-33 (the old timer fields):

Remove:
```python
    run_timer_hours: Mapped[int | None] = mapped_column(Integer)
    run_timer_max_ext_hours: Mapped[int | None] = mapped_column(Integer)
    run_timer_started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
```

Add:
```python
    auto_stop_minutes: Mapped[int | None] = mapped_column(Integer)
    auto_stop_started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    auto_stop_expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    auto_stop_warned: Mapped[bool] = mapped_column(default=False)
    auto_delete_minutes: Mapped[int | None] = mapped_column(Integer)
    auto_delete_started_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    auto_delete_warned: Mapped[bool] = mapped_column(default=False)
```

Keep `lifetime_expires_at` as-is (already exists at line 31-33).

Also add `Boolean` to the sqlalchemy imports at line 4:
```python
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
```

- [ ] **Step 5: Update Pydantic schemas**

In `src/backend/app/schemas/project.py`:

**ProjectCreate** — replace `run_timer_hours` with:
```python
    auto_stop_minutes: int | None = None
    auto_delete_minutes: int | None = None
```

**ProjectUpdate** — replace `run_timer_hours`, `run_timer_max_ext_hours`, `lifetime_expires_at` with:
```python
    auto_stop_minutes: int | None = None
    auto_delete_minutes: int | None = None
```

**ProjectResponse** — replace `run_timer_hours` with:
```python
    auto_stop_minutes: int | None = None
    auto_stop_expires_at: datetime.datetime | None = None
    auto_delete_minutes: int | None = None
    lifetime_expires_at: datetime.datetime | None = None
```

- [ ] **Step 6: Update GET /projects/{project_id} response dict**

In `src/backend/app/api/projects.py` at line 306, replace `"run_timer_hours": project.run_timer_hours,` with:
```python
        "auto_stop_minutes": project.auto_stop_minutes,
        "auto_stop_expires_at": project.auto_stop_expires_at.isoformat() if project.auto_stop_expires_at else None,
        "auto_delete_minutes": project.auto_delete_minutes,
```

Keep the existing `"lifetime_expires_at": project.lifetime_expires_at,` line (already there at line 307).

- [ ] **Step 7: Run the migration on dev DB**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m alembic upgrade head`

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_projects.py -v`
Expected: All tests pass, including the four new timer tests.

- [ ] **Step 9: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/models/project.py src/backend/app/schemas/project.py src/backend/app/api/projects.py
git add src/backend/app/models/project.py src/backend/app/schemas/project.py src/backend/app/api/projects.py src/backend/alembic/versions/ src/backend/tests/test_projects.py
git commit -m "feat: add auto-stop and auto-delete timer columns to project model"
```

---

### Task 2: Extend-Timer API Endpoint & Timer-Aware PATCH Logic

**Files:**
- Modify: `src/backend/app/api/projects.py` (PATCH handler at line 339, new endpoint)
- Test: `src/backend/tests/test_projects.py`

**Interfaces:**
- Consumes: `Project.auto_stop_minutes`, `Project.auto_stop_started_at`, `Project.auto_stop_expires_at`, `Project.auto_stop_warned`, `Project.auto_delete_minutes`, `Project.auto_delete_started_at`, `Project.auto_delete_warned`, `Project.lifetime_expires_at` from Task 1
- Produces: `POST /projects/{project_id}/extend-timer` endpoint accepting `{"timer": "auto_stop"|"auto_delete", "add_minutes": int}`. PATCH handler that recomputes `*_expires_at` when `auto_stop_minutes` or `auto_delete_minutes` changes, and clears timer fields when set to null.

- [ ] **Step 1: Write the failing tests**

Add to `src/backend/tests/test_projects.py`:

```python
import datetime


def test_patch_auto_stop_clears_expiry_when_disabled():
    """Setting auto_stop_minutes=None clears all auto-stop fields."""
    list_resp = client.get("/api/v1/projects", headers=HEADERS)
    project_id = list_resp.json()[0]["id"]

    # Set a timer
    client.patch(
        f"/api/v1/projects/{project_id}",
        json={"auto_stop_minutes": 60},
        headers=HEADERS,
    )
    # Manually set started_at to simulate an active project
    db = TestSession()
    from app.models.project import Project

    p = db.query(Project).filter_by(id=project_id).first()
    now = datetime.datetime.now(datetime.timezone.utc)
    p.auto_stop_started_at = now
    p.auto_stop_expires_at = now + datetime.timedelta(minutes=60)
    p.auto_stop_warned = True
    db.commit()
    db.close()

    # Disable the timer
    resp = client.patch(
        f"/api/v1/projects/{project_id}",
        json={"auto_stop_minutes": None},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["auto_stop_minutes"] is None
    assert data["auto_stop_expires_at"] is None


def test_patch_auto_stop_recomputes_expiry_when_running():
    """Changing auto_stop_minutes on an active timer recomputes expires_at."""
    list_resp = client.get("/api/v1/projects", headers=HEADERS)
    project_id = list_resp.json()[0]["id"]

    # Set up a running timer
    db = TestSession()
    from app.models.project import Project

    p = db.query(Project).filter_by(id=project_id).first()
    now = datetime.datetime.now(datetime.timezone.utc)
    p.auto_stop_minutes = 60
    p.auto_stop_started_at = now
    p.auto_stop_expires_at = now + datetime.timedelta(minutes=60)
    db.commit()
    db.close()

    # Change to 120 minutes
    resp = client.patch(
        f"/api/v1/projects/{project_id}",
        json={"auto_stop_minutes": 120},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["auto_stop_minutes"] == 120
    # expires_at should be ~120 min from started_at, not 60
    assert data["auto_stop_expires_at"] is not None


def test_extend_auto_stop_timer():
    list_resp = client.get("/api/v1/projects", headers=HEADERS)
    project_id = list_resp.json()[0]["id"]

    # Set up a running timer
    db = TestSession()
    from app.models.project import Project

    p = db.query(Project).filter_by(id=project_id).first()
    now = datetime.datetime.now(datetime.timezone.utc)
    p.auto_stop_minutes = 60
    p.auto_stop_started_at = now
    p.auto_stop_expires_at = now + datetime.timedelta(minutes=60)
    p.auto_stop_warned = True
    db.commit()
    old_expires = p.auto_stop_expires_at
    db.close()

    resp = client.post(
        f"/api/v1/projects/{project_id}/extend-timer",
        json={"timer": "auto_stop", "add_minutes": 30},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    # expires_at should be pushed forward by 30 min
    new_expires = datetime.datetime.fromisoformat(data["auto_stop_expires_at"])
    assert new_expires > old_expires


def test_extend_timer_fails_when_no_timer_active():
    create_resp = client.post(
        "/api/v1/projects", json={"name": "No Timer"}, headers=HEADERS
    )
    project_id = create_resp.json()["id"]
    resp = client.post(
        f"/api/v1/projects/{project_id}/extend-timer",
        json={"timer": "auto_stop", "add_minutes": 30},
        headers=HEADERS,
    )
    assert resp.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_projects.py::test_extend_auto_stop_timer -v`
Expected: FAIL — endpoint doesn't exist

- [ ] **Step 3: Add timer-aware logic to PATCH handler**

In `src/backend/app/api/projects.py`, modify the `update_project` function (line 339). After the existing `for field, value in fields.items():` setattr loop, add timer recomputation logic:

```python
@router.patch("/{project_id}")
def update_project(
    project_id: str,
    body: ProjectUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    fields = body.model_dump(exclude_unset=True)
    for field, value in fields.items():
        setattr(project, field, value)

    # Auto-stop timer recomputation
    if "auto_stop_minutes" in fields:
        if fields["auto_stop_minutes"] is None:
            project.auto_stop_started_at = None
            project.auto_stop_expires_at = None
            project.auto_stop_warned = False
        elif project.auto_stop_started_at:
            project.auto_stop_expires_at = (
                project.auto_stop_started_at
                + datetime.timedelta(minutes=project.auto_stop_minutes)
            )
            project.auto_stop_warned = False

    # Auto-delete timer recomputation
    if "auto_delete_minutes" in fields:
        if fields["auto_delete_minutes"] is None:
            project.auto_delete_started_at = None
            project.lifetime_expires_at = None
            project.auto_delete_warned = False
        elif project.auto_delete_started_at:
            project.lifetime_expires_at = (
                project.auto_delete_started_at
                + datetime.timedelta(minutes=project.auto_delete_minutes)
            )
            project.auto_delete_warned = False

    db.commit()
    db.refresh(project)
    if "topology" in fields:
        notify_project(
            project_id, {"type": "topology-update", "topology": project.topology}
        )
    return _project_response_dict(project)
```

Add `import datetime` at the top of the file if not already imported.

Also extract the GET response dict builder into a helper to avoid duplication (used by GET, PATCH, and extend-timer). Add near the top of the file:

```python
def _project_response_dict(project):
    result = {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "owner_id": project.owner_id,
        "provider_id": project.provider_id,
        "host_type": project.host_type,
        "host_id": project.host_id,
        "guid": project.guid,
        "state": project.state,
        "topology": project.topology,
        "deployed_topology": project.deployed_topology,
        "vni_map": project.vni_map,
        "deploy_error": project.deploy_error,
        "ocp_status": project.ocp_status,
        "ocp_install_elapsed": project.ocp_install_elapsed,
        "tags": project.tags,
        "auto_stop_minutes": project.auto_stop_minutes,
        "auto_stop_expires_at": project.auto_stop_expires_at.isoformat() if project.auto_stop_expires_at else None,
        "auto_delete_minutes": project.auto_delete_minutes,
        "lifetime_expires_at": project.lifetime_expires_at.isoformat() if project.lifetime_expires_at else None,
        "poweroff_mode": project.poweroff_mode,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }
    deployed_topo = project.deployed_topology or {}
    bmc_data = deployed_topo.get("bmc")
    if bmc_data:
        result["bmc"] = bmc_data
    return result
```

Update the GET handler to use `return _project_response_dict(project)` instead of the inline dict.

- [ ] **Step 4: Add the extend-timer endpoint**

In `src/backend/app/api/projects.py`, add after the PATCH handler:

```python
class ExtendTimerRequest(BaseModel):
    timer: str  # "auto_stop" or "auto_delete"
    add_minutes: int


@router.post("/{project_id}/extend-timer")
def extend_timer(
    project_id: str,
    body: ExtendTimerRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter_by(id=project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if project.owner_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Access denied")

    if body.timer == "auto_stop":
        if not project.auto_stop_expires_at:
            raise HTTPException(status_code=400, detail="Auto-stop timer is not active")
        project.auto_stop_expires_at += datetime.timedelta(minutes=body.add_minutes)
        project.auto_stop_warned = False
    elif body.timer == "auto_delete":
        if not project.lifetime_expires_at:
            raise HTTPException(status_code=400, detail="Auto-delete timer is not active")
        project.lifetime_expires_at += datetime.timedelta(minutes=body.add_minutes)
        project.auto_delete_warned = False
    else:
        raise HTTPException(status_code=400, detail="timer must be 'auto_stop' or 'auto_delete'")

    db.commit()
    db.refresh(project)
    return _project_response_dict(project)
```

Add `from pydantic import BaseModel` to the imports if `BaseModel` isn't already imported (it is via schemas, but `ExtendTimerRequest` is defined inline in the routes file).

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_projects.py -v`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/api/projects.py src/backend/tests/test_projects.py
git add src/backend/app/api/projects.py src/backend/tests/test_projects.py
git commit -m "feat: add extend-timer endpoint and timer-aware PATCH logic"
```

---

### Task 3: Timer Daemon Service & State Transition Hooks

**Files:**
- Create: `src/backend/app/services/project_timer.py`
- Modify: `src/backend/app/services/deploy_service.py` (lines 1789, 2676, 2844)
- Modify: `src/backend/app/main.py` (line 33)
- Test: `src/backend/tests/test_project_timer.py`

**Interfaces:**
- Consumes: `Project.auto_stop_minutes`, `Project.auto_stop_started_at`, `Project.auto_stop_expires_at`, `Project.auto_stop_warned`, `Project.auto_delete_minutes`, `Project.auto_delete_started_at`, `Project.auto_delete_warned`, `Project.lifetime_expires_at` from Task 1. `stop_project_async(project_id)` and `destroy_project_sync(ctx)` from deploy_service. `notify_project(project_id, message)` from ws_pubsub.
- Produces: `start_project_timer()` function (called from main.py). `_check_project_timers()` function that enforces expired timers and sends warnings. Timer fields set on state transitions in deploy_service.py.

- [ ] **Step 1: Write the failing tests**

Create `src/backend/tests/test_project_timer.py`:

```python
import datetime

from app.core.auth import create_jwt, hash_password
from app.models.project import Project
from app.models.user import User
from tests.conftest import TestSession

_db = TestSession()
_user = User(
    email="timer-test@example.com",
    display_name="Timer Test",
    role="user",
    auth_source="local",
    password_hash=hash_password("pass"),
)
_db.add(_user)
_db.commit()
_db.refresh(_user)
USER_ID = _user.id
_db.close()


def _create_project(name, **kwargs):
    db = TestSession()
    p = Project(name=name, owner_id=USER_ID, **kwargs)
    db.add(p)
    db.commit()
    db.refresh(p)
    pid = p.id
    db.close()
    return pid


def test_check_timers_fires_auto_stop():
    """Projects with expired auto_stop_expires_at and state=active get stopped."""
    from app.services.project_timer import _check_project_timers

    now = datetime.datetime.now(datetime.timezone.utc)
    pid = _create_project(
        "Auto Stop Test",
        state="active",
        auto_stop_minutes=60,
        auto_stop_started_at=now - datetime.timedelta(hours=2),
        auto_stop_expires_at=now - datetime.timedelta(hours=1),
    )

    stopped_ids = _check_project_timers(_dry_run=True)

    assert pid in stopped_ids["auto_stop"]

    # Clean up
    db = TestSession()
    db.query(Project).filter_by(id=pid).delete()
    db.commit()
    db.close()


def test_check_timers_fires_auto_delete():
    """Projects with expired lifetime_expires_at get deleted."""
    from app.services.project_timer import _check_project_timers

    now = datetime.datetime.now(datetime.timezone.utc)
    pid = _create_project(
        "Auto Delete Test",
        state="stopped",
        auto_delete_minutes=60,
        auto_delete_started_at=now - datetime.timedelta(hours=2),
        lifetime_expires_at=now - datetime.timedelta(hours=1),
    )

    result = _check_project_timers(_dry_run=True)

    assert pid in result["auto_delete"]

    # Clean up
    db = TestSession()
    db.query(Project).filter_by(id=pid).delete()
    db.commit()
    db.close()


def test_check_timers_skips_transitional_states():
    """Projects in deploying/stopping/starting/reconfiguring are skipped."""
    from app.services.project_timer import _check_project_timers

    now = datetime.datetime.now(datetime.timezone.utc)
    pid = _create_project(
        "Transitional Test",
        state="deploying",
        auto_stop_minutes=60,
        auto_stop_started_at=now - datetime.timedelta(hours=2),
        auto_stop_expires_at=now - datetime.timedelta(hours=1),
    )

    result = _check_project_timers(_dry_run=True)

    assert pid not in result["auto_stop"]

    # Clean up
    db = TestSession()
    db.query(Project).filter_by(id=pid).delete()
    db.commit()
    db.close()


def test_check_timers_sends_warning():
    """Projects within 5 min of expiry get a warning flag set."""
    from app.services.project_timer import _check_project_timers

    now = datetime.datetime.now(datetime.timezone.utc)
    pid = _create_project(
        "Warning Test",
        state="active",
        auto_stop_minutes=60,
        auto_stop_started_at=now - datetime.timedelta(minutes=57),
        auto_stop_expires_at=now + datetime.timedelta(minutes=3),
    )

    result = _check_project_timers(_dry_run=True)

    assert pid in result["auto_stop_warned"]

    # Clean up
    db = TestSession()
    db.query(Project).filter_by(id=pid).delete()
    db.commit()
    db.close()


def test_check_timers_no_double_warning():
    """Projects already warned are not warned again."""
    from app.services.project_timer import _check_project_timers

    now = datetime.datetime.now(datetime.timezone.utc)
    pid = _create_project(
        "No Double Warn",
        state="active",
        auto_stop_minutes=60,
        auto_stop_started_at=now - datetime.timedelta(minutes=57),
        auto_stop_expires_at=now + datetime.timedelta(minutes=3),
        auto_stop_warned=True,
    )

    result = _check_project_timers(_dry_run=True)

    assert pid not in result["auto_stop_warned"]

    # Clean up
    db = TestSession()
    db.query(Project).filter_by(id=pid).delete()
    db.commit()
    db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_project_timer.py::test_check_timers_fires_auto_stop -v`
Expected: FAIL — `project_timer` module doesn't exist

- [ ] **Step 3: Create the project timer service**

Create `src/backend/app/services/project_timer.py`:

```python
import datetime
import logging
import threading
import time

logger = logging.getLogger(__name__)

_INTERVAL_SECONDS = 30
_WARNING_MINUTES = 5
_TRANSITIONAL_STATES = frozenset(
    ("deploying", "stopping", "starting", "reconfiguring", "migrating")
)


def _check_project_timers(_dry_run=False):
    from app.core.database import SessionLocal
    from app.models.project import Project

    result = {
        "auto_stop": [],
        "auto_delete": [],
        "auto_stop_warned": [],
        "auto_delete_warned": [],
    }

    s = SessionLocal()
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        warning_threshold = now + datetime.timedelta(minutes=_WARNING_MINUTES)

        # 1. Expired auto-stop
        expired_stop = (
            s.query(Project)
            .filter(
                Project.auto_stop_expires_at <= now,
                Project.state == "active",
            )
            .all()
        )
        for p in expired_stop:
            result["auto_stop"].append(p.id)
            if _dry_run:
                continue
            logger.info(
                "Auto-stop fired for project %s (%s)", p.name, p.id[:8]
            )
            p.state = "stopping"
            p.auto_stop_started_at = None
            p.auto_stop_expires_at = None
            p.auto_stop_warned = False
            s.commit()
            _notify(p.id, {"type": "timer_fired", "timer": "auto_stop"})
            _spawn_stop(p.id)

        # 2. Expired auto-delete
        expired_delete = (
            s.query(Project)
            .filter(
                Project.lifetime_expires_at <= now,
                Project.state.in_(("active", "stopped", "error", "draft")),
            )
            .all()
        )
        for p in expired_delete:
            if p.state in _TRANSITIONAL_STATES:
                continue
            result["auto_delete"].append(p.id)
            if _dry_run:
                continue
            logger.info(
                "Auto-delete fired for project %s (%s)", p.name, p.id[:8]
            )
            _notify(p.id, {"type": "timer_fired", "timer": "auto_delete"})
            _delete_project(s, p)

        # 3. Auto-stop warning
        warn_stop = (
            s.query(Project)
            .filter(
                Project.auto_stop_expires_at <= warning_threshold,
                Project.auto_stop_expires_at > now,
                Project.auto_stop_warned == False,  # noqa: E712
                Project.state == "active",
            )
            .all()
        )
        for p in warn_stop:
            result["auto_stop_warned"].append(p.id)
            if _dry_run:
                continue
            remaining = (p.auto_stop_expires_at - now).total_seconds() / 60
            p.auto_stop_warned = True
            s.commit()
            _notify(
                p.id,
                {
                    "type": "timer_warning",
                    "timer": "auto_stop",
                    "expires_at": p.auto_stop_expires_at.isoformat(),
                    "minutes_remaining": round(remaining),
                },
            )

        # 4. Auto-delete warning
        warn_delete = (
            s.query(Project)
            .filter(
                Project.lifetime_expires_at <= warning_threshold,
                Project.lifetime_expires_at > now,
                Project.auto_delete_warned == False,  # noqa: E712
                Project.state.notin_(_TRANSITIONAL_STATES),
            )
            .all()
        )
        for p in warn_delete:
            result["auto_delete_warned"].append(p.id)
            if _dry_run:
                continue
            remaining = (p.lifetime_expires_at - now).total_seconds() / 60
            p.auto_delete_warned = True
            s.commit()
            _notify(
                p.id,
                {
                    "type": "timer_warning",
                    "timer": "auto_delete",
                    "expires_at": p.lifetime_expires_at.isoformat(),
                    "minutes_remaining": round(remaining),
                },
            )

    except Exception:
        logger.exception("Project timer check error")
        s.rollback()
    finally:
        s.close()

    return result


def _notify(project_id, message):
    try:
        from app.services.ws_pubsub import notify_project

        notify_project(project_id, message)
    except Exception:
        logger.warning("Failed to send timer notification for %s", project_id[:8])


def _spawn_stop(project_id):
    import threading

    from app.services.deploy_service import stop_project_async

    threading.Thread(
        target=stop_project_async,
        args=(project_id,),
        daemon=True,
        name=f"timer-stop-{project_id[:8]}",
    ).start()


def _delete_project(s, project):
    import copy

    from app.services.deploy_service import (
        destroy_project_sync,
        stop_project_async,
    )

    project_id = project.id

    if project.state == "active":
        project.state = "stopping"
        s.commit()
        stop_project_async(project_id)
        s.refresh(project)

    _notify(project_id, {"type": "project-deleted"})

    if project.host_id and project.state in ("stopped", "error"):
        destroy_ctx = {
            "project_id": project.id,
            "host_id": project.host_id,
            "vni_map": copy.deepcopy(project.vni_map or {}),
            "topology": copy.deepcopy(
                project.deployed_topology or project.topology or {}
            ),
            "dns_provider_id": project.dns_provider_id,
            "domain": project.domain,
        }
        import threading

        threading.Thread(
            target=destroy_project_sync,
            args=(destroy_ctx,),
            daemon=True,
            name=f"timer-destroy-{project_id[:8]}",
        ).start()

    from app.models.elastic_ip import ElasticIp
    from app.services.eip_service import release_eip

    project_eips = s.query(ElasticIp).filter_by(project_id=project_id).all()
    for eip in project_eips:
        try:
            release_eip(s, eip)
        except Exception:
            logger.warning("Failed to release EIP %s on timer delete", eip.public_ip)

    s.delete(project)
    s.commit()
    logger.info("Auto-delete complete for project %s", project_id[:8])


def _timer_loop():
    logger.info("Project timer started (interval=%ds)", _INTERVAL_SECONDS)
    while True:
        time.sleep(_INTERVAL_SECONDS)
        try:
            _check_project_timers()
        except Exception:
            logger.exception("Project timer loop error")


def start_project_timer():
    thread = threading.Thread(
        target=_timer_loop, daemon=True, name="project-timer"
    )
    thread.start()
    return thread
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_project_timer.py -v`
Expected: All 5 timer tests pass (using `_dry_run=True` so no actual stop/delete is triggered).

- [ ] **Step 5: Add state transition hooks in deploy_service.py**

In `src/backend/app/services/deploy_service.py`:

**After deploy completes** (line 1789, after `project.state = "active" if auto_start else "stopped"`):
```python
        project.state = "active" if auto_start else "stopped"
        project.deploy_error = None
        project.deploy_step = None
        project.deploy_progress = None
        project.deployed_topology = project.topology

        # Start auto-stop timer if configured
        if project.state == "active" and project.auto_stop_minutes:
            now = datetime.datetime.now(datetime.timezone.utc)
            project.auto_stop_started_at = now
            project.auto_stop_expires_at = now + datetime.timedelta(
                minutes=project.auto_stop_minutes
            )
            project.auto_stop_warned = False

        # Start auto-delete timer on first deploy
        if project.auto_delete_minutes and not project.auto_delete_started_at:
            now = datetime.datetime.now(datetime.timezone.utc)
            project.auto_delete_started_at = now
            project.lifetime_expires_at = now + datetime.timedelta(
                minutes=project.auto_delete_minutes
            )
            project.auto_delete_warned = False
```

Add `import datetime` to the top of deploy_service.py if not already there.

**After start completes** (line 2844, after `project.state = "active"`):
```python
        project.state = "active"
        project.deploy_error = None

        # Restart auto-stop timer
        if project.auto_stop_minutes:
            now = datetime.datetime.now(datetime.timezone.utc)
            project.auto_stop_started_at = now
            project.auto_stop_expires_at = now + datetime.timedelta(
                minutes=project.auto_stop_minutes
            )
            project.auto_stop_warned = False
```

**After stop completes** (line 2676, after `project.state = "stopped"`):
```python
        project.state = "stopped"
        project.deploy_error = None

        # Clear auto-stop timer (consumed; will restart on next start)
        project.auto_stop_started_at = None
        project.auto_stop_expires_at = None
        project.auto_stop_warned = False
```

- [ ] **Step 6: Register timer service in main.py**

In `src/backend/app/main.py`, add after `start_health_poller()` (line 33):

```python
    from app.services.project_timer import start_project_timer

    start_project_timer()
```

- [ ] **Step 7: Run full test suite**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 8: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/project_timer.py src/backend/app/services/deploy_service.py src/backend/app/main.py src/backend/tests/test_project_timer.py
git add src/backend/app/services/project_timer.py src/backend/app/services/deploy_service.py src/backend/app/main.py src/backend/tests/test_project_timer.py
git commit -m "feat: add project timer daemon and state transition hooks"
```

---

### Task 4: Frontend Timer Controls in PROJECT Palette

**Files:**
- Modify: `src/frontend/src/components/canvas/Palette.tsx` (line 540-548, PROJECT section)
- Modify: `src/frontend/src/app/projects/[id]/page.tsx` (pass timer props to Palette)

**Interfaces:**
- Consumes: `GET /projects/{id}` response with `auto_stop_minutes`, `auto_stop_expires_at`, `auto_delete_minutes`, `lifetime_expires_at`. `PATCH /projects/{id}` accepting `auto_stop_minutes` and `auto_delete_minutes`.
- Produces: Timer preset dropdowns and custom input in the PROJECT palette section. Calls PATCH API on change.

- [ ] **Step 1: Add timer state to the canvas page**

In `src/frontend/src/app/projects/[id]/page.tsx`, add state variables near the other project state (around line 30):

```typescript
const [autoStopMinutes, setAutoStopMinutes] = useState<number | null>(null);
const [autoDeleteMinutes, setAutoDeleteMinutes] = useState<number | null>(null);
const [autoStopExpiresAt, setAutoStopExpiresAt] = useState<string | null>(null);
const [lifetimeExpiresAt, setLifetimeExpiresAt] = useState<string | null>(null);
```

In the initial project fetch (around line 68), after `setProjectName(data.name)`:

```typescript
setAutoStopMinutes(data.auto_stop_minutes ?? null);
setAutoDeleteMinutes(data.auto_delete_minutes ?? null);
setAutoStopExpiresAt(data.auto_stop_expires_at ?? null);
setLifetimeExpiresAt(data.lifetime_expires_at ?? null);
```

Pass these to the Palette component (around line 679):

```typescript
{showPalette && <Palette
  onOpenStartOrder={() => setShowStartOrder(true)}
  onOpenExternalIps={() => setShowExternalIps(true)}
  projectDescription={projectDesc}
  projectGuid={projectGuid}
  projectId={projectId}
  hostId={isAdmin ? projectHostId : undefined}
  autoStopMinutes={autoStopMinutes}
  autoDeleteMinutes={autoDeleteMinutes}
  onAutoStopChange={(v) => {
    setAutoStopMinutes(v);
    fetch(`/api/v1/projects/${projectId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto_stop_minutes: v }),
    }).then(r => r.json()).then(data => {
      setAutoStopExpiresAt(data.auto_stop_expires_at ?? null);
    });
  }}
  onAutoDeleteChange={(v) => {
    setAutoDeleteMinutes(v);
    fetch(`/api/v1/projects/${projectId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto_delete_minutes: v }),
    }).then(r => r.json()).then(data => {
      setLifetimeExpiresAt(data.lifetime_expires_at ?? null);
    });
  }}
  // ...existing props
/>}
```

- [ ] **Step 2: Add timer controls to the Palette component**

In `src/frontend/src/components/canvas/Palette.tsx`, update the component props (line 173) to accept timer props:

```typescript
export default function Palette({
  onOpenStartOrder, onOpenExternalIps, projectDescription, projectGuid,
  onDescriptionChange, ocpHealth, projectId, hostId,
  autoStopMinutes, autoDeleteMinutes, onAutoStopChange, onAutoDeleteChange,
}: {
  onOpenStartOrder?: () => void;
  onOpenExternalIps?: () => void;
  projectDescription?: string;
  projectGuid?: string;
  onDescriptionChange?: (desc: string) => void;
  ocpHealth?: { phase: string; detail: string; items?: string[] } | null;
  projectId?: string;
  hostId?: string;
  autoStopMinutes?: number | null;
  autoDeleteMinutes?: number | null;
  onAutoStopChange?: (minutes: number | null) => void;
  onAutoDeleteChange?: (minutes: number | null) => void;
}) {
```

Add a local state for custom input mode:

```typescript
const [customStopOpen, setCustomStopOpen] = useState(false);
const [customDeleteOpen, setCustomDeleteOpen] = useState(false);
const [customStopH, setCustomStopH] = useState(0);
const [customStopM, setCustomStopM] = useState(0);
const [customDeleteH, setCustomDeleteH] = useState(0);
const [customDeleteM, setCustomDeleteM] = useState(0);
```

Add a preset helper above the component:

```typescript
const TIMER_PRESETS = [
  { label: "None", value: null },
  { label: "30m", value: 30 },
  { label: "1h", value: 60 },
  { label: "2h", value: 120 },
  { label: "4h", value: 240 },
  { label: "8h", value: 480 },
  { label: "24h", value: 1440 },
  { label: "Custom...", value: -1 },
] as const;

function formatMinutes(m: number): string {
  const h = Math.floor(m / 60);
  const min = m % 60;
  if (h && min) return `${h}h ${min}m`;
  if (h) return `${h}h`;
  return `${min}m`;
}

function currentPresetLabel(minutes: number | null | undefined): string {
  if (minutes == null) return "None";
  const preset = TIMER_PRESETS.find(p => p.value === minutes);
  return preset ? preset.label : formatMinutes(minutes);
}
```

In the PROJECT section (line 540-548), after the Start Order item, add the timer controls:

```tsx
{!collapsedSections.has("Project") && (
  <>
    <div className="palette-item" onClick={onOpenStartOrder} style={{ cursor: "pointer" }}>
      <div className="palette-icon" style={{ background: "rgba(108,99,255,0.15)" }}>🔢</div>
      <div>
        <div className="palette-item-label">Start Order</div>
        <div className="palette-item-desc">VM boot sequence</div>
      </div>
    </div>

    {/* Auto-Stop Timer */}
    <div className="palette-item" style={{ cursor: "default" }}>
      <div className="palette-icon" style={{ background: "rgba(251,191,36,0.15)" }}>⏱</div>
      <div style={{ flex: 1 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div className="palette-item-label">Auto-Stop</div>
          <select
            value={autoStopMinutes == null ? "null" : TIMER_PRESETS.some(p => p.value === autoStopMinutes) ? String(autoStopMinutes) : "-1"}
            onChange={(e) => {
              const v = e.target.value;
              if (v === "-1") { setCustomStopOpen(true); return; }
              setCustomStopOpen(false);
              onAutoStopChange?.(v === "null" ? null : Number(v));
            }}
            style={{
              fontSize: 10, padding: "1px 4px", borderRadius: 3,
              border: "1px solid var(--pf-t--global--border--color--default)",
              background: "var(--pf-t--global--background--color--secondary--default)",
              color: "var(--pf-t--global--text--color--regular)",
              maxWidth: 80,
            }}
          >
            {TIMER_PRESETS.map((p) => (
              <option key={String(p.value)} value={String(p.value)}>{p.label}</option>
            ))}
            {autoStopMinutes != null && !TIMER_PRESETS.some(p => p.value === autoStopMinutes) && (
              <option value={String(autoStopMinutes)}>{formatMinutes(autoStopMinutes)}</option>
            )}
          </select>
        </div>
        <div className="palette-item-desc">Stop VMs after duration</div>
        {customStopOpen && (
          <div style={{ display: "flex", alignItems: "center", gap: 4, marginTop: 4 }}>
            <input type="number" min={0} max={999} value={customStopH} onChange={e => setCustomStopH(Number(e.target.value))}
              style={{ width: 36, fontSize: 10, padding: "2px 4px", borderRadius: 3, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--secondary--default)", color: "var(--pf-t--global--text--color--regular)" }}
            />
            <span style={{ fontSize: 10, opacity: 0.6 }}>h</span>
            <input type="number" min={0} max={59} value={customStopM} onChange={e => setCustomStopM(Number(e.target.value))}
              style={{ width: 36, fontSize: 10, padding: "2px 4px", borderRadius: 3, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--secondary--default)", color: "var(--pf-t--global--text--color--regular)" }}
            />
            <span style={{ fontSize: 10, opacity: 0.6 }}>m</span>
            <button onClick={() => {
              const total = customStopH * 60 + customStopM;
              if (total > 0) { onAutoStopChange?.(total); setCustomStopOpen(false); }
            }}
              style={{ fontSize: 10, padding: "2px 6px", borderRadius: 3, border: "1px solid var(--pf-t--global--border--color--default)", background: "rgba(108,99,255,0.2)", color: "var(--pf-t--global--text--color--regular)", cursor: "pointer" }}
            >Set</button>
          </div>
        )}
      </div>
    </div>

    {/* Auto-Delete Timer */}
    <div className="palette-item" style={{ cursor: "default" }}>
      <div className="palette-icon" style={{ background: "rgba(239,68,68,0.15)" }}>🗑</div>
      <div style={{ flex: 1 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div className="palette-item-label">Auto-Delete</div>
          <select
            value={autoDeleteMinutes == null ? "null" : TIMER_PRESETS.some(p => p.value === autoDeleteMinutes) ? String(autoDeleteMinutes) : "-1"}
            onChange={(e) => {
              const v = e.target.value;
              if (v === "-1") { setCustomDeleteOpen(true); return; }
              setCustomDeleteOpen(false);
              onAutoDeleteChange?.(v === "null" ? null : Number(v));
            }}
            style={{
              fontSize: 10, padding: "1px 4px", borderRadius: 3,
              border: "1px solid var(--pf-t--global--border--color--default)",
              background: "var(--pf-t--global--background--color--secondary--default)",
              color: "var(--pf-t--global--text--color--regular)",
              maxWidth: 80,
            }}
          >
            {TIMER_PRESETS.map((p) => (
              <option key={String(p.value)} value={String(p.value)}>{p.label}</option>
            ))}
            {autoDeleteMinutes != null && !TIMER_PRESETS.some(p => p.value === autoDeleteMinutes) && (
              <option value={String(autoDeleteMinutes)}>{formatMinutes(autoDeleteMinutes)}</option>
            )}
          </select>
        </div>
        <div className="palette-item-desc">Delete project after duration</div>
        {customDeleteOpen && (
          <div style={{ display: "flex", alignItems: "center", gap: 4, marginTop: 4 }}>
            <input type="number" min={0} max={999} value={customDeleteH} onChange={e => setCustomDeleteH(Number(e.target.value))}
              style={{ width: 36, fontSize: 10, padding: "2px 4px", borderRadius: 3, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--secondary--default)", color: "var(--pf-t--global--text--color--regular)" }}
            />
            <span style={{ fontSize: 10, opacity: 0.6 }}>h</span>
            <input type="number" min={0} max={59} value={customDeleteM} onChange={e => setCustomDeleteM(Number(e.target.value))}
              style={{ width: 36, fontSize: 10, padding: "2px 4px", borderRadius: 3, border: "1px solid var(--pf-t--global--border--color--default)", background: "var(--pf-t--global--background--color--secondary--default)", color: "var(--pf-t--global--text--color--regular)" }}
            />
            <span style={{ fontSize: 10, opacity: 0.6 }}>m</span>
            <button onClick={() => {
              const total = customDeleteH * 60 + customDeleteM;
              if (total > 0) { onAutoDeleteChange?.(total); setCustomDeleteOpen(false); }
            }}
              style={{ fontSize: 10, padding: "2px 6px", borderRadius: 3, border: "1px solid var(--pf-t--global--border--color--default)", background: "rgba(239,68,68,0.2)", color: "var(--pf-t--global--text--color--regular)", cursor: "pointer" }}
            >Set</button>
          </div>
        )}
      </div>
    </div>
  </>
)}
```

- [ ] **Step 3: Test in browser**

Start the dev environment, open a project canvas, and verify:
1. PROJECT section shows Auto-Stop and Auto-Delete controls below Start Order
2. Dropdown presets work (selecting "1h" patches the API)
3. Custom input appears when "Custom..." is selected
4. Setting a custom value and clicking Set patches the API
5. Selecting "None" clears the timer

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka
git add src/frontend/src/components/canvas/Palette.tsx src/frontend/src/app/projects/\[id\]/page.tsx
git commit -m "feat: add auto-stop and auto-delete timer controls to canvas palette"
```

---

### Task 5: Action Bar Countdown Badge & Toast Notifications

**Files:**
- Modify: `src/frontend/src/app/projects/[id]/page.tsx` (action bar area, line 414-436)
- Modify: `src/frontend/src/hooks/useVmStateSocket.ts` (handle timer_warning and timer_fired)

**Interfaces:**
- Consumes: `autoStopExpiresAt` and `lifetimeExpiresAt` state from Task 4. WebSocket messages `timer_warning` and `timer_fired` from Task 3.
- Produces: Countdown badge in the action bar. Toast notification component for timer warnings with Extend button.

- [ ] **Step 1: Add timer message handling to useVmStateSocket**

In `src/frontend/src/hooks/useVmStateSocket.ts`, add to the VmStateSocket interface:

```typescript
timerWarning: { timer: string; expires_at: string; minutes_remaining: number } | null;
timerFired: string | null;  // "auto_stop" or "auto_delete"
```

Add state variables:
```typescript
const [timerWarning, setTimerWarning] = useState<VmStateSocket["timerWarning"]>(null);
const [timerFired, setTimerFired] = useState<string | null>(null);
```

Add cases in the message switch (after "project-deleted"):
```typescript
case "timer_warning":
  setTimerWarning({ timer: msg.timer, expires_at: msg.expires_at, minutes_remaining: msg.minutes_remaining });
  break;
case "timer_fired":
  setTimerFired(msg.timer);
  break;
```

Include `timerWarning` and `timerFired` in the return object.

- [ ] **Step 2: Add the countdown badge to the action bar**

In `src/frontend/src/app/projects/[id]/page.tsx`, add a countdown effect and display.

Add the countdown state near the other state variables:
```typescript
const [timerCountdown, setTimerCountdown] = useState<string | null>(null);
const [timerUrgency, setTimerUrgency] = useState<"normal" | "warning" | "critical">("normal");
```

Add a useEffect that ticks every second:
```typescript
useEffect(() => {
  const earliest = [autoStopExpiresAt, lifetimeExpiresAt]
    .filter(Boolean)
    .map(t => new Date(t!).getTime())
    .sort((a, b) => a - b)[0];

  if (!earliest) { setTimerCountdown(null); return; }

  const tick = () => {
    const remaining = earliest - Date.now();
    if (remaining <= 0) { setTimerCountdown("Expired"); setTimerUrgency("critical"); return; }
    const mins = Math.floor(remaining / 60000);
    const h = Math.floor(mins / 60);
    const m = mins % 60;
    setTimerCountdown(h > 0 ? `${h}h ${m}m` : `${m}m`);
    setTimerUrgency(mins <= 5 ? "critical" : mins <= 15 ? "warning" : "normal");
  };
  tick();
  const id = setInterval(tick, 1000);
  return () => clearInterval(id);
}, [autoStopExpiresAt, lifetimeExpiresAt]);
```

In the action bar (around line 434-436, after the project state badge):
```tsx
{timerCountdown && (
  <span
    className={`project-timer-badge ${timerUrgency}`}
    style={{
      fontSize: 11, marginLeft: 8, padding: "2px 8px", borderRadius: 10,
      color: timerUrgency === "critical" ? "#ef4444" : timerUrgency === "warning" ? "#fbbf24" : "#94a3b8",
      background: timerUrgency === "critical" ? "rgba(239,68,68,0.12)" : timerUrgency === "warning" ? "rgba(251,191,36,0.12)" : "rgba(148,163,184,0.08)",
      animation: timerUrgency === "critical" ? "pulse 1s infinite" : "none",
    }}
    title="Time remaining (click to open Project settings)"
    onClick={() => setShowPalette(true)}
  >
    ⏱ {timerCountdown}
  </span>
)}
```

- [ ] **Step 3: Add toast notification for timer warnings**

In `src/frontend/src/app/projects/[id]/page.tsx`, add toast state and rendering:

```typescript
const [timerToast, setTimerToast] = useState<{ timer: string; minutes: number } | null>(null);
```

Add a useEffect to listen for WebSocket timer warnings:
```typescript
useEffect(() => {
  if (ws.timerWarning) {
    setTimerToast({ timer: ws.timerWarning.timer, minutes: ws.timerWarning.minutes_remaining });
  }
}, [ws.timerWarning]);
```

Add a useEffect to listen for timer_fired (updates project state):
```typescript
useEffect(() => {
  if (ws.timerFired === "auto_stop") {
    setProjectState("stopping");
  } else if (ws.timerFired === "auto_delete") {
    setProjectState("deleting");
    router.push("/projects");
  }
}, [ws.timerFired]);
```

Add the toast component just inside the outermost div, above the action bar:
```tsx
{timerToast && (
  <div style={{
    position: "fixed", top: 16, left: "50%", transform: "translateX(-50%)", zIndex: 9999,
    background: timerToast.timer === "auto_delete" ? "rgba(239,68,68,0.95)" : "rgba(251,191,36,0.95)",
    color: "#fff", padding: "10px 20px", borderRadius: 8,
    display: "flex", alignItems: "center", gap: 12, fontSize: 13, fontWeight: 500,
    boxShadow: "0 4px 20px rgba(0,0,0,0.3)",
  }}>
    <span>
      {timerToast.timer === "auto_stop" ? "⏱ Auto-stop" : "🗑 Auto-delete"} in {timerToast.minutes} minute{timerToast.minutes !== 1 ? "s" : ""}
    </span>
    <button
      onClick={() => {
        fetch(`/api/v1/projects/${projectId}/extend-timer`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ timer: timerToast.timer, add_minutes: 60 }),
        }).then(r => r.json()).then(data => {
          setAutoStopExpiresAt(data.auto_stop_expires_at ?? null);
          setLifetimeExpiresAt(data.lifetime_expires_at ?? null);
        });
        setTimerToast(null);
      }}
      style={{
        padding: "4px 12px", borderRadius: 4, border: "1px solid rgba(255,255,255,0.4)",
        background: "rgba(255,255,255,0.15)", color: "#fff", cursor: "pointer", fontSize: 12,
      }}
    >Extend 1h</button>
    <button
      onClick={() => setTimerToast(null)}
      style={{
        padding: "4px 8px", borderRadius: 4, border: "none",
        background: "transparent", color: "rgba(255,255,255,0.7)", cursor: "pointer", fontSize: 14,
      }}
    >✕</button>
  </div>
)}
```

- [ ] **Step 4: Add pulse animation CSS**

In the canvas page's existing styles or a global CSS file, ensure this keyframe exists (add inline if needed):

In the page component's JSX, add a style tag if one doesn't exist:
```tsx
<style>{`
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
  }
`}</style>
```

- [ ] **Step 5: Test in browser**

Start the dev environment:
1. Set an auto-stop timer on an active project — verify the countdown badge appears in the action bar
2. Verify color escalation (set a short timer like 2m to test critical/red)
3. Verify the toast appears when the WebSocket warning arrives (set timer to 6m, wait 1 min)
4. Verify "Extend 1h" button on the toast calls the API and updates the countdown
5. Verify "Dismiss" closes the toast
6. Verify timer_fired redirects to projects list for auto-delete

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka
git add src/frontend/src/hooks/useVmStateSocket.ts src/frontend/src/app/projects/\[id\]/page.tsx
git commit -m "feat: add countdown badge and toast warnings for project timers"
```
