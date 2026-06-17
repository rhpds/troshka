# Troshka Installation — Common Setup

This guide covers the backend, frontend, database, and configuration setup that is common to all cloud providers. Complete this guide first, then proceed to your provider-specific installation guide:

- [AWS](install-aws.md)
- [GCP](install-gcp.md)
- [Azure](install-azure.md)
- [OCP Virt](install-ocpvirt.md)

## Prerequisites

Before installing Troshka, ensure you have the following:

- **Python 3.11+** — required for the backend API and services
- **Node.js 20+** — required for the Next.js frontend
- **PostgreSQL 16** — or podman/docker to run it in a container (recommended for development)
- **podman or docker** — for running PostgreSQL in development mode
- **git** — to clone the repository

Verify your Python and Node.js versions:

```bash
python3 --version  # Must be 3.11 or higher
node --version     # Must be 20.x or higher
```

## Clone Repository

Clone the Troshka repository and navigate to the project root:

```bash
git clone https://github.com/rhpds/troshka.git
cd troshka
```

## Backend Setup

### Install Python Dependencies

Create a Python virtual environment and install the backend dependencies:

```bash
cd src/backend
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

The backend uses FastAPI, SQLAlchemy 2.0, Alembic for migrations, and Dynaconf for configuration management.

## Configuration

Troshka's configuration is managed by `src/backend/config/config.yaml`. This file defines all default settings for the application.

### Configuration Sections

The configuration file contains the following sections:

#### `app` — Application settings

- `name`: Application name (default: `troshka`)
- `port`: Backend API port (default: `8200`)
- `host`: Bind address (default: `0.0.0.0`)
- `log_level`: Logging level (default: `info`)
- `root_path`: Root path for reverse proxy deployments (default: `""`)
- `external_url`: Public URL for webhooks and callbacks (default: `""`)

#### `database` — Database connection

- `url`: PostgreSQL connection string (default: `postgresql+psycopg2://troshka:troshka@localhost:5433/troshka`)

#### `redis` — Redis cache

- `url`: Redis connection string (default: `redis://localhost:6379/0`)

#### `auth` — Authentication and authorization

- `jwt_secret`: Secret key for JWT signing (default: `CHANGE-ME-IN-LOCAL-CONFIG`)
- `jwt_algorithm`: JWT algorithm (default: `HS256`)
- `jwt_expiry_hours`: JWT token expiry in hours (default: `24`)
- `oauth_enabled`: Enable OIDC authentication (default: `false`)
- `allow_registration`: Allow new user registration (default: `true`)
- `admin_users`: Comma-separated list of admin user emails (default: `prutledg@redhat.com`)
- `operator_users`: Comma-separated list of operator user emails (default: `""`)
- `allowed_groups`: Comma-separated list of allowed OIDC groups (default: `""`)
- `admin_groups`: Comma-separated list of admin OIDC groups (default: `""`)
- `operator_groups`: Comma-separated list of operator OIDC groups (default: `""`)

#### `aws` — AWS default settings

- `default_region`: Default AWS region (default: `us-east-1`)
- `default_instance_type`: Default EC2 instance type for hosts (default: `m8i.xlarge`)

#### `defaults` — Resource limits and defaults

- `run_timer_hours`: Default VM runtime hours (default: `8`)
- `lifetime_days`: Default project lifetime in days (default: `30`)
- `max_vms_per_project`: Maximum VMs per project (default: `20`)
- `max_projects_per_user`: Maximum projects per user (default: `10`)
- `user_library_quota_gb`: User library quota in GB (default: `500`)

#### `overcommit` — Resource overcommit ratios

- `cpu_ratio`: CPU overcommit ratio (default: `4.0`)
- `ram_ratio`: RAM overcommit ratio (default: `1.5`)

#### `gc` — Garbage collector settings

- `cache_stale_hours_patterns`: Hours before patterns are considered stale (default: `24`)
- `cache_stale_hours_snapshots`: Hours before snapshots are considered stale (default: `1`)
- `cache_stale_hours_images`: Hours before library images are considered stale (default: `5`)

#### `health` — Health check settings

- `interval_seconds`: Health check interval in seconds (default: `30`)
- `disconnect_after_seconds`: Mark host disconnected after N seconds (default: `90`)

### Overriding Configuration

You can override configuration values in three ways (in order of precedence):

1. **Environment variables** — Use `TROSHKA_*` prefix with `__` as section separator:

   ```bash
   export TROSHKA_AUTH__JWT_SECRET=my-secret-key
   export TROSHKA_DATABASE__URL=postgresql+psycopg2://user:pass@host:5432/db
   export TROSHKA_APP__PORT=9000
   ```

2. **Local configuration file** — Create `config.local.yaml` in the same directory as `config.yaml`:

   ```yaml
   auth:
     jwt_secret: my-secret-key
     oauth_enabled: true
     admin_users: "admin@example.com"
   
   database:
     url: "postgresql+psycopg2://user:pass@host:5432/db"
   ```

   The `config.local.yaml` file is gitignored and will not be committed to the repository.

3. **Default configuration** — Values from `config.yaml` are used if no overrides are set.

## Database Setup

### Development Mode with Podman

For development, run PostgreSQL 16 in a podman container with a persistent volume:

```bash
podman run -d --name troshka-postgres \
  --restart=always \
  -e POSTGRES_USER=troshka \
  -e POSTGRES_PASSWORD=troshka \
  -e POSTGRES_DB=troshka \
  -p 5433:5432 \
  -v troshka-pgdata:/var/lib/postgresql/data \
  docker.io/library/postgres:16
```

The `--restart=always` flag ensures the container restarts after system reboots or crashes.

**WARNING:** Never run `podman rm troshka-postgres` — this destroys the container link to the persistent volume. To fully reset the database, use:

```bash
podman stop troshka-postgres
podman rm troshka-postgres
podman volume rm troshka-pgdata
```

### Run Database Migrations

After starting PostgreSQL, run Alembic migrations to create the database schema:

```bash
cd src/backend
./venv/bin/python3 -m alembic upgrade head
```

This creates all tables, indexes, and constraints required by Troshka.

### Production Database

For production deployments, use a managed PostgreSQL 16 instance (AWS RDS, GCP Cloud SQL, Azure Database for PostgreSQL, etc.) and update the `database.url` setting in your `config.local.yaml`:

```yaml
database:
  url: "postgresql+psycopg2://user:password@host:5432/database"
```

## Frontend Setup

Install the Next.js frontend dependencies:

```bash
cd src/frontend
npm install
```

The frontend uses Next.js 15 with the App Router, PatternFly 6, React Flow, and Zustand for state management.

## Development Mode

Troshka includes a development script (`dev-services.sh`) that manages the backend, frontend, and PostgreSQL services.

### Start All Services

Start the database, backend, and frontend in one command:

```bash
./dev-services.sh start
```

This will:
- Start PostgreSQL in a podman container (if not already running)
- Run database migrations
- Start the backend API on port 8200
- Start the frontend on port 3100

### Service URLs

| Service | URL | Notes |
|---------|-----|-------|
| Frontend | http://localhost:3100 | Hot-reloads automatically |
| Backend API | http://localhost:8200 | No auto-reload — restart required for Python changes |
| API Documentation | http://localhost:8200/docs | Auto-generated Swagger UI |
| PostgreSQL | localhost:5433 | Podman container |

### Start Individual Services

Start services individually:

```bash
./dev-services.sh backend start   # Start backend only
./dev-services.sh frontend start  # Start frontend only
./dev-services.sh db start        # Start PostgreSQL only
```

### Restart Services

Restart the backend after making Python code changes:

```bash
./dev-services.sh restart backend
```

The frontend hot-reloads automatically — no restart needed for changes to files in `src/frontend/`.

### Stop Services

Stop all services:

```bash
./dev-services.sh stop
```

Stop individual services:

```bash
./dev-services.sh stop backend
./dev-services.sh stop frontend
./dev-services.sh stop db
```

### Check Service Status

View the status of all services:

```bash
./dev-services.sh status
```

### Backend Logs

Backend and frontend logs are written to `/tmp/`:

```bash
tail -f /tmp/troshka-backend.log
tail -f /tmp/troshka-frontend.log
```

## S3 Storage

Troshka requires S3-compatible storage for:
- Library ISO and disk image uploads
- Pattern storage and distribution
- Snapshot exports

### Configuration

S3 can be configured in two ways:

1. **Via Provider** (recommended for production) — Create an S3 provider in the admin UI (Admin → Providers → Add S3 Provider) with:
   - `region`: AWS region (e.g., `us-east-1`)
   - `access_key_id`: AWS access key ID
   - `secret_access_key`: AWS secret access key
   - `bucket`: S3 bucket name (e.g., `troshka-images`)
   - `endpoint_url`: Custom endpoint URL (optional, for MinIO or other S3-compatible services)

2. **Via `config.local.yaml`** (for development) — Add an `s3` section:

   ```yaml
   s3:
     region: us-east-1
     access_key_id: YOUR_ACCESS_KEY_ID
     secret_access_key: YOUR_SECRET_ACCESS_KEY
     bucket: troshka-images
     endpoint_url: ""  # Leave empty for AWS S3, or set to MinIO/R2/Wasabi endpoint
   ```

The backend will use the S3 provider from the database if available, falling back to the `config.yaml` settings if no active S3 provider is configured.

## Production Deployment

### Backend

Run the backend with uvicorn in production mode. Example systemd unit file:

```ini
[Unit]
Description=Troshka Backend API
After=network.target postgresql.service

[Service]
Type=simple
User=troshka
WorkingDirectory=/opt/troshka/src/backend
Environment="PATH=/opt/troshka/src/backend/venv/bin"
ExecStart=/opt/troshka/src/backend/venv/bin/uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8200 \
  --workers 4
Restart=always

[Install]
WantedBy=multi-user.target
```

### Reverse Proxy

Use nginx or another reverse proxy to terminate TLS and proxy requests to the backend. Example nginx configuration:

```nginx
upstream troshka_backend {
    server 127.0.0.1:8200;
}

server {
    listen 443 ssl http2;
    server_name troshka.example.com;

    ssl_certificate /etc/letsencrypt/live/troshka.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/troshka.example.com/privkey.pem;

    # Frontend (static files from Next.js build)
    location / {
        root /opt/troshka/src/frontend/out;
        try_files $uri $uri/ /index.html;
    }

    # Backend API
    location /api/ {
        proxy_pass http://troshka_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # WebSocket support for real-time updates
    location /ws/ {
        proxy_pass http://troshka_backend;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400;
    }
}
```

### OIDC Authentication

For production deployments, enable OIDC authentication by setting `oauth_enabled: true` and configuring:

- `admin_users`: Comma-separated list of admin user emails
- `admin_groups`: OIDC groups that grant admin access
- `operator_groups`: OIDC groups that grant operator (read-only) access
- `allowed_groups`: OIDC groups that grant user access

Example production auth configuration:

```yaml
auth:
  jwt_secret: "generated-secret-key"  # Use a secure random key
  oauth_enabled: true
  allow_registration: false
  admin_users: "admin@example.com"
  admin_groups: "troshka-admins"
  operator_groups: "troshka-operators"
  allowed_groups: "troshka-users"
```

In dev mode (`oauth_enabled: false`), all requests are automatically authenticated as the first admin user.

## Troubleshooting

### Port 5433 Already in Use

If you see an error that port 5433 is already in use, check for other PostgreSQL instances:

```bash
podman ps -a | grep postgres
lsof -i :5433
```

Stop any conflicting services or change the port in `dev-services.sh` and `config.yaml`.

### Alembic Migration Errors

If `alembic upgrade head` fails:

1. Verify PostgreSQL is running: `podman ps | grep troshka-postgres`
2. Test the database connection:
   ```bash
   podman exec -it troshka-postgres psql -U troshka -d troshka -c "SELECT version();"
   ```
3. Check the connection string in `config.yaml` or your `config.local.yaml`
4. Ensure the database user has CREATE privileges

### Authentication Issues in Development

If you see authentication errors in development mode:

1. Verify `oauth_enabled: false` is set in `config.yaml` or overridden in `config.local.yaml`
2. Restart the backend: `./dev-services.sh restart backend`
3. Check backend logs: `tail -f /tmp/troshka-backend.log`

### Backend Won't Start

If the backend fails to start:

1. Check Python version: `python3 --version` (must be 3.11+)
2. Verify all dependencies are installed: `cd src/backend && ./venv/bin/pip install -r requirements.txt`
3. Check for missing environment variables or invalid configuration
4. Review backend logs: `tail -f /tmp/troshka-backend.log`

### PostgreSQL Container Issues

If the PostgreSQL container stops unexpectedly:

1. Check podman/docker logs: `podman logs troshka-postgres`
2. Verify the data volume exists: `podman volume ls | grep troshka-pgdata`
3. Check disk space: `df -h`
4. Restart the container: `podman start troshka-postgres`

If the volume is corrupted, you may need to reset it:

```bash
podman stop troshka-postgres
podman rm troshka-postgres
podman volume rm troshka-pgdata
# Then re-run the podman run command from the Database Setup section
```

## Next Steps

After completing this common setup, proceed to your cloud provider's installation guide:

- [AWS Installation Guide](install-aws.md) — Set up AWS credentials, IAM policies, and VPC
- [GCP Installation Guide](install-gcp.md) — Set up GCP project, service account, and networking
- [Azure Installation Guide](install-azure.md) — Set up Azure subscription, service principal, and resource groups
- [OCP Virt Installation Guide](install-ocpvirt.md) — Set up OpenShift Virtualization cluster access

These guides cover provider-specific configuration, credential setup, and host provisioning.
