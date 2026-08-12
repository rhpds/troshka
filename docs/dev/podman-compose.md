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
- **Local `libvirt` provider host can't reach the compose MinIO by default.** If you register a
  [`libvirt`-type host](../../infra/libvirt-host-image/README.md) (e.g. a nested VM on your own
  machine's default `virbr0` network) instead of a real cloud host, `troshkad` running there
  downloads library images from this stack's MinIO — but two things block that out of the box:
  1. The compose-internal hostname `http://minio:9000` doesn't resolve outside the podman network.
     Point an S3 `Provider` (Admin > Providers, or the API) at the host's libvirt gateway IP
     instead — typically `http://192.168.124.1:9000` for the default NAT network (check with
     `ip -4 addr show virbr0`). A DB-configured provider takes priority over the env var fallback,
     so no restart is needed.

     In **Admin > Providers > + Add Provider**:
     - **Type**: `S3 Storage`
     - **S3 Bucket**: `troshka-images` (must match the compose stack's bucket)
     - Check **S4 / Custom S3 Endpoint**, then set **Endpoint URL** to the gateway IP, e.g.
       `http://192.168.124.1:9000`
     - **Access Key** / **Secret Key**: `minioadmin` / `minioadmin` (the compose MinIO defaults)
  2. firewalld's `libvirt` zone (assigned to `virbr0`) rejects inbound traffic to arbitrary ports by
     default — it only allows `dhcp`/`dhcpv6`/`dns`/`ssh`/`tftp`. Don't open the port directly in the
     shared `libvirt` zone — libvirt places **every** NAT/route/open network's bridge into that same
     zone, so doing so would expose MinIO to any other libvirt guest on the machine too, not just the
     Troshka host VM. Instead, give this network its own zone
     ([libvirt docs](https://libvirt.org/firewall.html#firewalld-and-the-virtual-network-driver)):

     ```bash
     # 1. Create a dedicated zone instead of modifying the shared `libvirt` one
     sudo firewall-cmd --permanent --new-zone=troshka-libvirt

     # 2. Re-add what guests need by default (the built-in `libvirt` zone pre-allows these;
     #    a fresh custom zone starts empty) plus the MinIO port
     sudo firewall-cmd --permanent --zone=troshka-libvirt \
       --add-service=dhcp --add-service=dhcpv6 --add-service=dns \
       --add-service=tftp --add-service=ssh --add-port=9000/tcp
     sudo firewall-cmd --reload

     # 3. Point the network's bridge at the new zone — edit XML, don't use
     #    `--change-interface`, since libvirt re-applies its own zone assignment on
     #    every network start and would overwrite an ad hoc interface move
     sudo virsh net-edit default   # or whichever network the libvirt host VM uses
     # add zone='troshka-libvirt' to the <bridge> element, e.g.:
     #   <bridge name='virbr0' zone='troshka-libvirt'/>
     sudo virsh net-destroy default && sudo virsh net-start default
     ```

     This affects **all** guests on that network (e.g. `default`), not just the Troshka VM — if the
     dev machine runs other unrelated libvirt VMs on the same network, they move out of the shared
     `libvirt` zone into `troshka-libvirt` too. For a fully isolated blast radius, use a separate
     dedicated libvirt network just for the Troshka host VM instead of `default`.

     To undo this and put `default` back on the standard `libvirt` zone (e.g. once you no longer
     need the local libvirt provider):

     ```bash
     sudo virsh net-edit default
     # remove the zone='troshka-libvirt' attribute from the <bridge> element, e.g.:
     #   <bridge name='virbr0' zone='troshka-libvirt'/>  ->  <bridge name='virbr0'/>
     sudo virsh net-destroy default && sudo virsh net-start default

     # optional cleanup — only if nothing else was moved into troshka-libvirt
     sudo firewall-cmd --permanent --delete-zone=troshka-libvirt
     sudo firewall-cmd --reload
     ```

  Verify with `curl http://192.168.124.1:9000/minio/health/live` from inside the libvirt host (e.g.
  via `./scripts/host-ssh.sh`).
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
