# Podman Compose Dev Stack

An alternative to [`dev-services.sh`](../../dev-services.sh) that runs the whole local dev stack —
Postgres, Redis, backend, worker, and frontend — as containers via `podman compose`, with source
bind-mounted into each container so both the backend (`uvicorn --reload`) and frontend (`next dev`)
hot-reload on file changes. The compose stack also runs a local MinIO container (see
[Image library storage](#image-library-storage-minio) below), which `dev-services.sh` doesn't.

Use whichever path you prefer. They are **not meant to run at the same time**.

## Prerequisites

- Podman with the `podman compose` subcommand (or `podman-compose` installed separately)

## Starting the stack

```bash
podman compose up -d
```

This builds the backend/frontend dev images (see `deploy/containerfiles/Containerfile.backend.dev`
and `Containerfile.frontend.dev`), starts Postgres + Redis + MinIO, waits for them to report
healthy, runs `alembic upgrade head`, then starts the backend, worker, and frontend.

| Service | URL | Notes |
|---------|-----|-------|
| Frontend | http://localhost:3100 | `next dev`, hot-reloads |
| Backend API | http://localhost:8200 | `uvicorn --reload`, hot-reloads |
| API Docs | http://localhost:8200/docs | Swagger UI |
| PostgreSQL | localhost:5433 | container `postgres` |
| Redis | localhost:6379 | container `redis` |
| MinIO API | localhost:9000 | S3-compatible storage for the image library |
| MinIO Console | http://localhost:9001 | Web UI — login `minioadmin` / `minioadmin` |

## Image library storage (MinIO)

The image library (ISOs/disk images, `Library` > `Images`) needs an S3-compatible backend. The
`podman compose` stack runs a local MinIO container out of the box, so uploads work immediately with
**no Admin > Providers setup required** — the backend picks it up automatically via
`TROSHKA_S3__*` env vars (`endpoint_url`, `region`, `access_key_id`, `secret_access_key`, `bucket`),
which [`s3_storage._get_s3_config()`](../../src/backend/app/services/s3_storage.py) falls back to
when no `Provider(type="s3")` row exists in the database. The `troshka-images` bucket is created
automatically on backend startup if it doesn't already exist.

Because the browser can't resolve the container-network hostname `minio` (compose) directly, uploads
against this local MinIO go through a single-request proxy endpoint (`/{item_id}/upload-proxy`)
instead of the usual presigned-URL multipart flow — the UI switches between the two automatically
based on whether the resolved S3 config has an `endpoint_url` set.

If you want to test against real AWS S3 instead, add an S3 `Provider` via **Admin > Providers** in
the UI — a DB-configured provider always takes priority over the MinIO env var fallback.

## Common commands

```bash
# Run more than one background worker (dev-services.sh defaults to 3)
podman compose up -d --scale worker=3

# Tail logs for one service
podman compose logs -f backend

# Rebuild after changing requirements.txt, pyproject.toml, or package.json
podman compose build backend worker frontend
podman compose up -d

# Stop everything (containers only — volumes are kept)
podman compose down

# Wipe the compose Postgres data too
podman compose down -v
```

## Caveats

- **Port collisions with `dev-services.sh`.** Both stacks bind the same host ports (`5433`, `6379`,
  `8200`, `3100`). Stop one before starting the other — `./dev-services.sh stop` or
  `podman compose down`. The compose stack additionally binds `9000`/`9001` for MinIO, which
  `dev-services.sh` doesn't use.
- **Separate Postgres data.** The compose stack uses its own named volume
  (`troshka-compose-pgdata`), independent from the `troshka-pgdata` volume that
  `dev-services.sh` manages via `troshka-postgres`. Data doesn't automatically move between the two
  — if you need to bring data over, dump/restore it manually with `pg_dump`/`pg_restore`.
- **Rebuilds aren't automatic.** Python/Node dependency changes (`requirements.txt`,
  `pyproject.toml`, `package.json`) require an explicit `build` — the bind mounts only pick up
  source file changes, not dependency changes baked into the image layers.
- **`troshkad` / operator / real hosts are out of scope.** This stack covers the API + UI dev loop
  only. Deploying actual VMs still requires a connected libvirt host — see
  [`docs/dev/host-agent.md`](host-agent.md).
- **Fresh-database migrations.** The backend command runs `alembic upgrade head || true`, matching
  `dev-services.sh`'s leniency — on a truly from-scratch database this can currently skip over a
  pre-existing gap in the migration history (a handful of revisions are missing a `down_revision`
  link back to the migration that creates the `projects` table). This is not caused by the compose
  setup; it's just newly visible here because compose is often the first time anyone bootstraps a
  fully empty database instead of reusing a long-lived local Postgres volume.

## Notes on the container setup

- Bind mounts use the `:z` SELinux relabel option so containers can read source on
  SELinux-enforcing hosts (harmless no-op if SELinux is disabled/permissive).
- `frontend` runs as root (container-local only) because `next dev` needs to write its `.next`
  build cache into the bind-mounted source tree, which is owned by the host user's UID rather than
  the image's default UID. `backend`/`worker` don't need this — Python silently skips writing
  `.pyc` caches when it lacks permission, so they run as the image's default non-root user.
