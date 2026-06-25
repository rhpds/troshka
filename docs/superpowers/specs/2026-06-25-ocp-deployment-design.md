# Troshka OCP Deployment Design

Deploy the Troshka backend and frontend as container images on OpenShift, installable via Ansible playbook or raw kustomize manifests.

## Container Images

Two images published to `quay.io/rhpds/`:

### troshka-backend

- **Base**: `registry.access.redhat.com/ubi9/python-311`
- **Contents**: `src/backend/` (FastAPI app) + `src/troshkad/troshkad.py` (for agent version hash and push updates)
- **Entrypoint**: `uvicorn app.main:app --host 0.0.0.0 --port 8200`
- **Config**: Dynaconf reads `config.yaml` from ConfigMap mount, with `TROSHKA_*` env var overrides
- **Dependencies**: installed from `requirements.txt` (includes cloud provider SDKs: boto3, google-cloud-*, azure-*)
- **Tags**: git SHA + `latest` on main, semver on tags

### troshka-frontend

- **Build stage**: `registry.access.redhat.com/ubi9/nodejs-18` — `npm ci && npm run build` with `output: 'standalone'` in next.config.ts
- **Runtime stage**: `ubi9/ubi-minimal` + Node.js runtime — copies `.next/standalone`, `.next/static`, `public/`
- **Entrypoint**: `node server.js`
- **Config**: `BACKEND_URL` env var points to backend ClusterIP Service (e.g., `http://troshka-backend:8200`)
- **Tags**: same as backend

The Containerfile sets `ENV STANDALONE=true` during build. `next.config.ts` checks `process.env.STANDALONE === 'true'` and adds `output: 'standalone'` only then. Local dev (no env var) is unaffected.

## Deployment Architecture

```
                    ┌─────────────────┐
                    │   OCP Route     │
                    │ (edge TLS)      │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              │ (SSO)        │ (no SSO)     │
              ▼              ▼              │
     ┌────────────────┐                     │
     │  oauth-proxy   │                     │
     │  Deployment    │                     │
     │  (optional)    │                     │
     └───────┬────────┘                     │
             │ X-Forwarded-Email/User       │
             ▼                              ▼
     ┌────────────────┐            ┌────────────────┐
     │   Frontend     │            │   Frontend     │
     │   Deployment   │            │   Deployment   │
     │   (Next.js)    │            │   (Next.js)    │
     └───────┬────────┘            └───────┬────────┘
             │ /api/v1/* proxy             │
             ▼                             ▼
     ┌────────────────┐            ┌────────────────┐
     │   Backend      │            │   Backend      │
     │   Deployment   │            │   Deployment   │
     │   (FastAPI)    │            │   (FastAPI)    │
     └───────┬────────┘            └───────┬────────┘
             │                             │
             ▼                             ▼
     ┌────────────────┐            ┌────────────────┐
     │  PostgreSQL    │            │  PostgreSQL    │
     │  (external or  │            │  (external or  │
     │   in-cluster)  │            │   in-cluster)  │
     └────────────────┘            └────────────────┘
```

With SSO: Route → oauth-proxy → frontend → backend
Without SSO: Route → frontend → backend (dev mode auto-auth)

The frontend is the only externally-exposed service. It proxies `/api/v1/*` to the backend via Next.js rewrites. The backend Service is ClusterIP only.

## Kustomize Layout

```
deploy/
├── base/
│   ├── kustomization.yaml
│   ├── namespace.yaml
│   ├── backend-deployment.yaml
│   ├── frontend-deployment.yaml
│   ├── frontend-route.yaml
│   ├── backend-config.yaml
│   ├── backend-secret.yaml
│   └── alembic-job.yaml
├── overlays/
│   ├── sso/
│   │   ├── kustomization.yaml
│   │   ├── oauth-proxy-deployment.yaml
│   │   ├── oauth-proxy-secret.yaml
│   │   ├── oauth-client.yaml
│   │   ├── oauth-serviceaccount.yaml
│   │   └── frontend-route-patch.yaml
│   ├── postgres/
│   │   ├── kustomization.yaml
│   │   ├── postgres-statefulset.yaml
│   │   ├── postgres-service.yaml
│   │   └── backend-secret-patch.yaml
│   └── minio/
│       ├── kustomization.yaml
│       ├── minio-deployment.yaml
│       ├── minio-service.yaml
│       └── backend-config-patch.yaml
```

Kustomize overlays include `../../base` so each overlay is self-contained (`oc apply -k deploy/overlays/sso` deploys everything).

## Ansible Playbook

Primary installation method. Uses `kubernetes.core.k8s` with inline Jinja2 templates — no kustomize dependency on the control node.

### Directory Structure

```
deploy/ansible/
├── deploy.yaml
├── undeploy.yaml
├── inventory/
│   └── group_vars/
│       └── all.yaml
└── tasks/
    ├── namespace.yaml
    ├── secrets.yaml
    ├── database.yaml
    ├── minio.yaml
    ├── oauth.yaml
    ├── backend.yaml
    ├── frontend.yaml
    └── migrate.yaml
```

### Variables

```yaml
# Required
troshka_namespace: troshka

# Images
troshka_backend_image: quay.io/rhpds/troshka-backend:latest
troshka_frontend_image: quay.io/rhpds/troshka-frontend:latest

# Route
troshka_route_host: ""  # auto-generated from namespace if empty

# Database — provide URL or deploy in-cluster
troshka_db_url: ""                  # external PostgreSQL connection string
troshka_deploy_postgres: false      # deploy PostgreSQL StatefulSet
troshka_postgres_storage_size: 10Gi
troshka_postgres_image: registry.redhat.io/rhel9/postgresql-16:latest

# S3 — provide creds or deploy MinIO
troshka_s3_bucket: troshka-images
troshka_s3_access_key: ""
troshka_s3_secret_key: ""
troshka_s3_endpoint: ""             # custom S3 endpoint (for MinIO or non-AWS)
troshka_deploy_minio: false
troshka_minio_storage_size: 50Gi

# Auth
troshka_oauth_enabled: false
troshka_admin_users: ""             # CSV of admin emails
troshka_operator_users: ""          # CSV of operator emails

# Auto-generated secrets (idempotent — only created if not present)
# troshka_jwt_secret       — 32 random bytes, base64
# troshka_encryption_key   — 32 random bytes, base64 (Fernet)
# troshka_oauth_client_secret
# troshka_oauth_cookie_secret
```

### Idempotency

- **Secrets**: check `oc get secret troshka-secrets` — if exists, read existing values. If not, generate and create. Never overwrites on re-run.
- **OAuthClient**: same check-before-create pattern.
- **Deployments/Services/Routes**: `kubernetes.core.k8s` with `state: present` (always idempotent).
- **Migration Job**: unique name per run (timestamp suffix), waits for completion, cleans up old completed jobs.

### Example Usage

```bash
# All-in-one with SSO
ansible-playbook deploy/ansible/deploy.yaml \
  -e troshka_deploy_postgres=true \
  -e troshka_deploy_minio=true \
  -e troshka_oauth_enabled=true \
  -e troshka_admin_users=prutledg@redhat.com

# External DB, no SSO
ansible-playbook deploy/ansible/deploy.yaml \
  -e troshka_db_url="postgresql://user:pass@db.example.com:5432/troshka"

# Teardown
ansible-playbook deploy/ansible/undeploy.yaml
```

## Authentication

### SSO Mode (oauth_enabled: true)

- Ansible creates an `OAuthClient` CR with auto-generated secrets
- Creates a ServiceAccount with `serviceaccounts.openshift.io/oauth-redirectreference.primary` annotation
- Deploys `ose-oauth-proxy-rhel9` as a separate Deployment
- Route points to oauth-proxy Service → proxies to frontend
- oauth-proxy injects `X-Forwarded-Email` and `X-Forwarded-User` headers
- Backend reads these headers via existing `_upsert_sso_user()` flow
- Backend config: `TROSHKA_AUTH__OAUTH_ENABLED=true`

### Local Auth Mode (oauth_enabled: false)

- No oauth-proxy deployed
- Route points directly to frontend
- Backend auto-authenticates as admin (existing dev-mode behavior)
- Backend config: `TROSHKA_AUTH__OAUTH_ENABLED=false` (default)

## Database Migration

Alembic migrations run as a Kubernetes Job before the backend Deployment starts:

1. Ansible creates a Job (`troshka-migrate-{timestamp}`)
2. Job uses the backend image with command: `alembic upgrade head`
3. Job mounts the same DB Secret as the backend
4. Ansible waits for Job completion (timeout: 120s)
5. Old completed migration Jobs cleaned up (keep last 3)

The backend Deployment has an `initContainer` that waits for the DB to be reachable before starting uvicorn.

## Backend ConfigMap

The ConfigMap provides a minimal `config.yaml` with values templated by Ansible:

```yaml
app:
  port: 8200
  host: "0.0.0.0"
  external_url: "https://{{ troshka_route_host }}"

auth:
  oauth_enabled: {{ troshka_oauth_enabled }}
  admin_users: "{{ troshka_admin_users }}"
  operator_users: "{{ troshka_operator_users }}"

defaults:
  run_timer_hours: {{ troshka_run_timer_hours | default(8) }}
  lifetime_days: {{ troshka_lifetime_days | default(30) }}
```

Sensitive values (DB URL, JWT secret, encryption key, S3 creds) are in the Secret, injected as env vars with `TROSHKA_` prefix.

## CI/CD

GitHub Actions workflow `.github/workflows/build-images.yaml`:

- **Trigger**: push to `main`, tags matching `v*`
- **Jobs**: `build-backend` and `build-frontend` (parallel)
- **Build**: `podman build -f deploy/containerfiles/Containerfile.backend` (and `.frontend`)
- **Push**: `podman push` to `quay.io/rhpds/troshka-{backend,frontend}`
- **Tags**: `:{git-sha}` + `:latest` on main pushes, `:{semver}` on tag pushes
- **Secrets**: `QUAY_USERNAME`, `QUAY_PASSWORD` repository secrets

## File Inventory

New files to create:

```
deploy/
├── containerfiles/
│   ├── Containerfile.backend
│   └── Containerfile.frontend
├── base/
│   ├── kustomization.yaml
│   ├── namespace.yaml
│   ├── backend-deployment.yaml
│   ├── frontend-deployment.yaml
│   ├── frontend-route.yaml
│   ├── backend-config.yaml
│   ├── backend-secret.yaml
│   └── alembic-job.yaml
├── overlays/
│   ├── sso/
│   │   ├── kustomization.yaml
│   │   ├── oauth-proxy-deployment.yaml
│   │   ├── oauth-proxy-secret.yaml
│   │   ├── oauth-client.yaml
│   │   ├── oauth-serviceaccount.yaml
│   │   └── frontend-route-patch.yaml
│   ├── postgres/
│   │   ├── kustomization.yaml
│   │   ├── postgres-statefulset.yaml
│   │   ├── postgres-service.yaml
│   │   └── backend-secret-patch.yaml
│   └── minio/
│       ├── kustomization.yaml
│       ├── minio-deployment.yaml
│       ├── minio-service.yaml
│       └── backend-config-patch.yaml
├── ansible/
│   ├── deploy.yaml
│   ├── undeploy.yaml
│   ├── inventory/
│   │   └── group_vars/
│   │       └── all.yaml
│   └── tasks/
│       ├── namespace.yaml
│       ├── secrets.yaml
│       ├── database.yaml
│       ├── minio.yaml
│       ├── oauth.yaml
│       ├── backend.yaml
│       ├── frontend.yaml
│       └── migrate.yaml
.github/workflows/build-images.yaml
```

Modified files:
- `src/frontend/next.config.ts` — add conditional `output: 'standalone'`

## Out of Scope

- Horizontal pod autoscaling (single replica is fine initially)
- Ingress/NetworkPolicy (OCP Routes handle external access)
- Backup/restore automation for PostgreSQL
- Image signing or vulnerability scanning
- Operator packaging (future consideration)
