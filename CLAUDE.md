# Troshka Development Guide

## Architecture

Nested VM environment builder: FastAPI backend + Next.js frontend + libvirt host agents.

- **Backend**: `src/backend/` — Python 3.11, FastAPI, SQLAlchemy 2, Alembic, Dynaconf
- **Frontend**: `src/frontend/` — Next.js 15 (App Router), PatternFly 6, React Flow, Zustand
- **Workers**: `src/backend/app/workers/` — RQ workers for background deploy/destroy/start/stop jobs
- **Config**: `src/backend/config/config.yaml` (overrides: `config.local.yaml`, env vars `TROSHKA_*`)
- **Database**: PostgreSQL 16 (port 5433 in dev), SQLite for tests
- **Redis**: Job queue + shared state + pub/sub (optional — falls back to in-memory when unavailable)
## Dev Environment

```bash
./dev-services.sh start          # Start everything (PostgreSQL + Redis + backend + worker + frontend)
./dev-services.sh restart backend # Restart backend only (frontend hot-reloads)
./dev-services.sh restart worker  # Restart RQ worker only
./scripts/host-ssh.sh            # SSH into first connected host (credentials from DB)
./scripts/host-ssh.sh -- <cmd>   # Run command on host
./scripts/host-db.sh             # Interactive Python shell with DB session + models
./scripts/host-db.sh "<code>"    # Run inline DB query
./scripts/update-agent.sh       # Push troshkad update via API (fast, stamps version)
./scripts/reinstall-agent.sh    # Full SSH reinstall (for broken agents)
./scripts/dev-tunnel.sh start   # Expose local backend to remote hosts (ops pods / agnosticd callbacks)
./scripts/dev-tunnel.sh stop    # Tear down the tunnel + restore local external_url
```

- Backend: http://localhost:8200 (no auto-reload — restart required for Python changes)
- Frontend: http://localhost:3100 (hot-reloads)
- Dev mode auto-authenticates as admin

### Remote callbacks in dev (ops pods, agnosticd)

Ops pods and agnosticd run on a REMOTE host and call back to the Troshka API via
`app.external_url` (`TROSHKA_API_URL`). On a laptop behind NAT, `localhost` is
unreachable from the host. `./scripts/dev-tunnel.sh start` opens a public tunnel
to `:8200` (cloudflared quick-tunnel, zero-config — `brew install cloudflared`;
ngrok also supported), writes the tunnel URL to `app.external_url` in
`config.local.yaml`, and restarts backend + worker so it takes effect. Run `stop`
to restore `external_url` to the local value.
## Running Tests

```bash
cd src/backend && ./venv/bin/python3 -m pytest tests/ -v
```

Tests use SQLite with type compiler overrides for JSONB/UUID. Auth is dev-mode (auto-authenticates).

**CI and dev both run Python 3.13.** Always add extra trailing values to `time.time()` mocks to prevent `StopIteration`.
### Git Commands — ALWAYS Use Absolute Paths

Never `cd` into a subdirectory and then run `git add` with relative paths — this doubles the path segment and fails. Always use one of:

```bash
# Option 1: absolute path (preferred)
git add /Users/prutledg/troshka/src/backend/app/api/file.py

# Option 2: cd to project root first
cd /Users/prutledg/troshka && git add src/backend/app/api/file.py

# Option 3: git status --short to see actual paths, then use those
```
## Code Quality

SonarQube enforces a quality gate on every push via GitLab CI. Key rule: **cognitive complexity must stay at or below 15 per function** (rule S3776). When adding logic to an existing function, check if it's already near the limit — extract helpers rather than nesting deeper.
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
- Background jobs run via RQ workers: `enqueue_job(func, *args)` from `app.core.redis`
- When Redis unavailable: falls back to daemon threads (same behavior as before RQ)
- Progress tracking: Redis keys (`set_progress` / `get_progress` / `delete_progress`)
- Host operations: `troshkad_client.start_job()` / `poll_job()` / `wait_for_job()` / `cancel_job()`
### Frontend Pages
- `"use client"` directive on all pages
- Raw `fetch()` for API calls (no TanStack Query)
- `useState` + `useEffect` for state management
- PatternFly components: `PageSection`, `Toolbar`, `Card`, `Button`
### Canvas
- Topology stored as JSONB in `Project.topology` (source of truth)
- Zustand store: `useCanvasStore` for nodes, edges, selections
- Node types: `vmNode`, `networkNode`, `storageNode`, `containerNode` (single containers AND pods)
- Auto-save: debounced 1s after changes via `_saveTopologyToApi`
- Empty canvas (draft, no nodes) shows "Import Template YAML" overlay — palette still interactive behind it
## Important Conventions
### Library System
- User libraries use `type="personal"` (NOT `type="user"`)
- Always use `_ensure_user_library()` or `Library.filter_by(type="personal")`
### VNI Allocation
- VNIs are globally unique across all projects (for multi-host VXLAN peering)
- Monotonically increasing, never recycled — high-water mark persisted to `.vni_hwm` file
- Never use the `Network.vni` column (it's unused)
### Topology Remapping (Patterns/Deploy)
- When cloning topology, remap ALL ID references:
  - Node IDs, edge source/target, edge sourceHandle/targetHandle
  - NIC IDs + MACs, disk controller IDs
  - `bootDevices[]` (storage node IDs)
  - `startOrder[].vmId`, `startOrder[].waitForVm`
  - `externalIps[].vmId`, `hiddenNodeIds[]`
### Duplicate Name Prevention
- Projects, patterns, library items, and snapshots enforce unique names per user
- Frontend pre-checks before destructive operations (e.g., check before VM shutdown for snapshot)
### Dev Database
- PostgreSQL runs in a podman container (`troshka-postgres`) with persistent volume (`troshka-pgdata`)
- `--restart=always` ensures container restarts after crashes
- Never `podman rm` the container — data persists on the named volume but rm destroys the link
- To fully reset: `podman volume rm troshka-pgdata` (intentional data loss)
## Database Migrations

```bash
cd src/backend
./venv/bin/python3 -m alembic revision -m "description"
./venv/bin/python3 -m alembic upgrade head
```

Head revision chain is in `src/backend/alembic/versions/`. FK columns must use `postgresql.UUID(as_uuid=False)` to match the existing schema (not `String(36)`).

## Building Container Images

CI (`.github/workflows/build-images.yml`) builds/pushes all images on push to `main` — normally you just push code. For **manual/local** builds, do NOT build on the local (arm64) podman machine: cross-arch emulation is slow and OOM-prone (native modules like wetty/node-pty). Build natively on an amd64 Troshka host instead:

```bash
# one image (streams context over SSH, builds with sudo podman, optional push)
./scripts/build-image-on-host.sh --host <id> \
  --context src/operator/images/terminal \
  --tag quay.io/redhat-gpte/troshka-terminal:latest \
  --build-arg OCP_VERSION=stable --push

# all images (or a subset by short name)
./scripts/build-all-images-on-host.sh --host <id>            # all, push
./scripts/build-all-images-on-host.sh --host <id> terminal   # just one
```

Push reuses your local `podman login` auth (`~/.config/containers/auth.json`), streamed to the host and removed after. Get `<id>` from `./scripts/host-ssh.sh --list`.

## Detailed References

Deep per-subsystem reference lives in `docs/dev/` (read on demand — not loaded every session):

- [`docs/dev/providers.md`](docs/dev/providers.md) — AWS, GCP, Azure, OCP Virt, KubeVirt Native, Red Hat Image Builder setup
- [`docs/dev/host-agent.md`](docs/dev/host-agent.md) — troshkad daemon internals, host operations, libvirt events, Virtual BMC, health poller
- [`docs/dev/networking.md`](docs/dev/networking.md) — multi-host WireGuard/VXLAN mesh, PXE boot, VNC console + Route53, OCP Routes, oc-exec DNS
- [`docs/dev/storage.md`](docs/dev/storage.md) — shared storage & live migration, storage auto-extend, garbage collector
- [`docs/dev/deploy-pipeline.md`](docs/dev/deploy-pipeline.md) — deploy pipeline, clock backdating, cloud-init, AgnosticD-v2, pull-through registry, template import/export
- [`docs/dev/network-router-bootstrap.md`](docs/dev/network-router-bootstrap.md) — vrnetlab vs Troshka day-0 config (ISO vs serial per NOS), net-automation workshop rtr1–rtr4
- [`docs/dev/subsystems.md`](docs/dev/subsystems.md) — Redis job queue, group access control, exec API, container/pod nodes, registry creds, timers, WS pubsub, offline FS, pattern save, DNS providers
- [`docs/dev/deployment.md`](docs/dev/deployment.md) — Helm chart, Kustomize overlays, Ansible playbooks, container images
