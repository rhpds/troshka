# Troshka Development Guide

## Architecture

Nested VM environment builder: FastAPI backend + Next.js frontend + libvirt host agents.

- **Backend**: `src/backend/` — Python 3.11, FastAPI, SQLAlchemy 2, Alembic, Dynaconf
- **Frontend**: `src/frontend/` — Next.js 15 (App Router), PatternFly 6, React Flow, Zustand
- **Config**: `src/backend/config/config.yaml` (overrides: `config.local.yaml`, env vars `TROSHKA_*`)
- **Database**: PostgreSQL 16 (port 5433 in dev), SQLite for tests

## Dev Environment

```bash
./dev-services.sh start          # Start everything (PostgreSQL + backend + frontend)
./dev-services.sh restart backend # Restart backend only (frontend hot-reloads)
```

- Backend: http://localhost:8200 (no auto-reload — restart required for Python changes)
- Frontend: http://localhost:3100 (hot-reloads)
- Dev mode auto-authenticates as admin

## Running Tests

```bash
cd src/backend
./venv/bin/python3 -m pytest tests/ -v
```

Tests use SQLite with type compiler overrides for JSONB/UUID. Auth is dev-mode (auto-authenticates).

## Key Patterns

### Backend Models (SQLAlchemy 2.0+)
- `Mapped[type]` + `mapped_column()` syntax
- UUIDs as strings: `UUID(as_uuid=False), default=lambda: str(uuid.uuid4())`
- Relationships: `back_populates`, `cascade="all, delete-orphan"` for children
- Register new models in `src/backend/app/models/__init__.py`

### Backend API Routes
- Router: `APIRouter(prefix="/resource", tags=["resource"])`
- Auth: `user: User = Depends(get_current_user)`
- Async operations: spawn `threading.Thread(daemon=True)`, never block HTTP requests
- Register new routers in `src/backend/app/main.py`

### Backend Services
- Function-based modules (not classes)
- Background threads get fresh DB sessions: `SessionLocal()`
- Progress tracking: module-level dicts (e.g., `_deploy_progress`)
- SSH to hosts: `run_ssh_script(host_ip, private_key, script, timeout)`

### Frontend Pages
- `"use client"` directive on all pages
- Raw `fetch()` for API calls (no TanStack Query)
- `useState` + `useEffect` for state management
- PatternFly components: `PageSection`, `Toolbar`, `Card`, `Button`

### Canvas
- Topology stored as JSONB in `Project.topology` (source of truth)
- Zustand store: `useCanvasStore` for nodes, edges, selections
- Node types: `vmNode`, `networkNode`, `storageNode`
- Auto-save: debounced 1s after changes via `_saveTopologyToApi`

## Important Conventions

### Library System
- User libraries use `type="personal"` (NOT `type="user"`)
- Always use `_ensure_user_library()` or `Library.filter_by(type="personal")`

### VNI Allocation
- VNIs are globally unique across all projects (for future multi-host VXLAN peering)
- Allocated by scanning all `Project.vni_map` JSONB fields
- Never use the `Network.vni` column (it's unused)

### Topology Remapping (Patterns/Deploy)
- When cloning topology, remap ALL ID references:
  - Node IDs, edge source/target, edge sourceHandle/targetHandle
  - NIC IDs + MACs, disk controller IDs
  - `bootDevices[]` (storage node IDs)
  - `startOrder[].vmId`, `startOrder[].waitForVm`
  - `externalIps[].vmId`, `hiddenNodeIds[]`

### Cloud-Init
- Seed ISO with NoCloud datasource (cidata volume label)
- `instance-id` must be unique per deploy (UUID suffix) for cloud-init to re-run
- `chpasswd` uses new `users:` format (not deprecated `list: |`)
- Custom user-data is YAML-validated before appending

### Host Operations
- Disk paths: `/var/lib/troshka/vms/{project_id}/{vm_id[:8]}-{disk_id[:8]}.{format}`
- Image cache: `/var/lib/troshka/images/{item_id}.{format}`
- Pattern cache: `/var/lib/troshka/cache/patterns/{pattern_id}/`
- Snapshot cache: `/var/lib/troshka/cache/snapshots/{item_id}/`
- Domain names: `troshka-{project_id[:8]}-{vm_id[:8]}`
- Flatten qcow2 before S3 upload (merge backing chain for standalone images)

### Garbage Collector
- Runs on host agent connect, admin Clean button, or future cron
- Steps: capacity sync → orphan cleanup → network repair → cache eviction
- Cache eviction configurable per type in `config.yaml` (`gc.cache_stale_hours_*`)

### Duplicate Name Prevention
- Projects, patterns, library items, and snapshots enforce unique names per user
- Frontend pre-checks before destructive operations (e.g., check before VM shutdown for snapshot)

## Database Migrations

```bash
cd src/backend
./venv/bin/python3 -m alembic revision -m "description"
./venv/bin/python3 -m alembic upgrade head
```

Head revision chain is in `src/backend/alembic/versions/`. FK columns must use `postgresql.UUID(as_uuid=False)` to match the existing schema (not `String(36)`).
