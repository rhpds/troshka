# Troshka Installation — OpenShift (Container Deployment)

Deploy the Troshka backend and frontend as containers on any OpenShift 4.x cluster. This is a separate concept from *OCP Virt as a host provider* — this guide installs the Troshka application itself on OCP.

For running Troshka outside containers (bare-metal, VM), see [Common Setup](install-common.md) instead.

## Prerequisites

- OpenShift 4.x cluster with `oc` CLI access
- Ansible 2.14+ with `kubernetes.core` collection
- `oc login` completed (or kubeconfig configured)

Install the Ansible collection if you don't have it:

```bash
ansible-galaxy collection install kubernetes.core
```

## Quick Start

### All-in-One (built-in PostgreSQL + MinIO, no SSO)

The fastest way to get Troshka running — deploys everything in a single namespace:

```bash
ansible-playbook deploy/ansible/deploy.yaml \
  -e troshka_deploy_postgres=true \
  -e troshka_deploy_minio=true \
  -e troshka_admin_users=you@example.com
```

This creates:
- `troshka` namespace
- PostgreSQL 16 StatefulSet (10Gi PVC)
- MinIO S3-compatible storage (50Gi PVC)
- Backend Deployment (FastAPI/uvicorn)
- Frontend Deployment (Next.js standalone)
- OCP Route with edge TLS

All passwords (PostgreSQL, MinIO, JWT, encryption key) are auto-generated and stored in a Kubernetes Secret. They persist across playbook re-runs.

Open the Route URL — Troshka auto-authenticates as admin in dev mode.

### With SSO (Red Hat SSO / OpenShift OAuth)

For production with cluster SSO authentication:

```bash
ansible-playbook deploy/ansible/deploy.yaml \
  -e troshka_deploy_postgres=true \
  -e troshka_deploy_minio=true \
  -e troshka_oauth_enabled=true \
  -e troshka_admin_users=admin@redhat.com
```

This additionally deploys:
- `ose-oauth-proxy` Deployment (authenticates via OpenShift OAuth)
- `OAuthClient` CR (auto-generated client + cookie secrets)
- ServiceAccount with OAuth redirect annotation
- Route points to oauth-proxy → frontend → backend

Users log in via the cluster's OAuth provider. The proxy injects `X-Forwarded-Email` and `X-Forwarded-User` headers. Admin/operator roles are assigned based on the `troshka_admin_users` and `troshka_operator_users` CSV lists.

### External Database + S3 (no built-in services)

Point to an existing PostgreSQL and S3:

```bash
ansible-playbook deploy/ansible/deploy.yaml \
  -e troshka_db_url="postgresql+psycopg2://user:pass@db.example.com:5432/troshka" \
  -e troshka_s3_access_key=YOUR_ACCESS_KEY \
  -e troshka_s3_secret_key=YOUR_SECRET_KEY \
  -e troshka_oauth_enabled=true \
  -e troshka_admin_users=admin@redhat.com
```

## Configuration Variables

All variables have defaults in `deploy/ansible/inventory/group_vars/all.yaml`.

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `troshka_namespace` | `troshka` | OCP namespace to deploy into |
| `troshka_backend_image` | `quay.io/redhat-gpte/troshka-backend:latest` | Backend container image |
| `troshka_frontend_image` | `quay.io/redhat-gpte/troshka-frontend:latest` | Frontend container image |
| `troshka_route_host` | auto-generated | Route hostname (auto: `troshka-{ns}.apps.{cluster}`) |

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `troshka_db_url` | `""` | External PostgreSQL URL (if not deploying in-cluster) |
| `troshka_deploy_postgres` | `false` | Deploy PostgreSQL StatefulSet in-cluster |
| `troshka_postgres_storage_size` | `10Gi` | PVC size for PostgreSQL |
| `troshka_postgres_image` | `registry.redhat.io/rhel9/postgresql-16:latest` | PostgreSQL image |
| `troshka_postgres_password` | auto-generated | PostgreSQL password (generated if empty) |

### S3 Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `troshka_s3_bucket` | `troshka-images` | S3 bucket name |
| `troshka_s3_access_key` | `""` | S3 access key (defaults to MinIO user if deploying MinIO) |
| `troshka_s3_secret_key` | `""` | S3 secret key (defaults to MinIO password if deploying MinIO) |
| `troshka_s3_endpoint` | `""` | Custom S3 endpoint URL |
| `troshka_deploy_minio` | `false` | Deploy MinIO in-cluster |
| `troshka_minio_storage_size` | `50Gi` | PVC size for MinIO |

### Authentication

| Variable | Default | Description |
|----------|---------|-------------|
| `troshka_oauth_enabled` | `false` | Enable SSO via ose-oauth-proxy |
| `troshka_admin_users` | `""` | CSV of admin user emails |
| `troshka_operator_users` | `""` | CSV of operator user emails |

### Resource Limits

| Variable | Default | Description |
|----------|---------|-------------|
| `troshka_backend_memory_limit` | `2Gi` | Backend memory limit |
| `troshka_backend_cpu_request` | `250m` | Backend CPU request |
| `troshka_frontend_memory_limit` | `512Mi` | Frontend memory limit |
| `troshka_frontend_cpu_request` | `100m` | Frontend CPU request |

## What Gets Deployed

### Base (always)

| Resource | Type | Purpose |
|----------|------|---------|
| `troshka` | Namespace | Isolation |
| `troshka-backend` | Deployment + Service | FastAPI API server (port 8200, ClusterIP) |
| `troshka-frontend` | Deployment + Service | Next.js UI (port 3000, ClusterIP) |
| `troshka` | Route | Edge-TLS entry point (frontend or oauth-proxy) |
| `troshka-config` | ConfigMap | Non-sensitive configuration (config.yaml) |
| `troshka-secrets` | Secret | DB URL, JWT secret, encryption key, S3 creds, passwords |
| `troshka-migrate-*` | Job | Alembic database migration |

### Optional: PostgreSQL (`troshka_deploy_postgres: true`)

| Resource | Type | Purpose |
|----------|------|---------|
| `troshka-postgres` | StatefulSet | PostgreSQL 16 (1 replica) |
| `troshka-postgres` | Service (headless) | Database endpoint |
| `data-troshka-postgres-0` | PVC | Persistent storage |

### Optional: MinIO (`troshka_deploy_minio: true`)

| Resource | Type | Purpose |
|----------|------|---------|
| `troshka-minio` | Deployment | S3-compatible object store |
| `troshka-minio` | Service | S3 API (9000) + console (9001) |
| `troshka-minio-data` | PVC | Object storage |
| `troshka-minio-init` | Job | Creates the default bucket |

### Optional: SSO (`troshka_oauth_enabled: true`)

| Resource | Type | Purpose |
|----------|------|---------|
| `troshka-oauth-proxy` | Deployment + Service | ose-oauth-proxy (port 8443) |
| `troshka-oauth` | ServiceAccount | OAuth redirect annotation |
| `troshka-{ns}` | OAuthClient | Cluster OAuth client registration |
| `troshka-oauth-proxy` | Secret | Client + cookie secrets |
| `troshka-oauth-tls` | Secret (auto) | Serving cert (OCP service-cert annotation) |

## Secrets Management

All secrets are auto-generated on first deploy and **never overwritten** on subsequent runs:

- **JWT secret** — 48-char random string
- **Encryption key** — 44-char random string (Fernet, for pull secret / token encryption)
- **PostgreSQL password** — 24-char random string
- **MinIO password** — 24-char random string
- **OAuth client secret** — 32-char random string
- **OAuth cookie secret** — 32-byte random (base64-encoded)

To rotate secrets, delete the `troshka-secrets` Secret and re-run the playbook:

```bash
oc delete secret troshka-secrets -n troshka
ansible-playbook deploy/ansible/deploy.yaml -e ...
```

## Architecture

```
                 ┌──────────────┐
                 │  OCP Route   │
                 │ (edge TLS)   │
                 └──────┬───────┘
                        │
           ┌────────────┤ (SSO only)
           ▼            ▼
    ┌─────────────┐  ┌─────────────┐
    │ oauth-proxy │  │  Frontend   │
    │ (optional)  │──│  (Next.js)  │
    └─────────────┘  └──────┬──────┘
                            │ /api/v1/* proxy
                            ▼
                     ┌─────────────┐
                     │   Backend   │
                     │  (FastAPI)  │
                     └──────┬──────┘
                            │
              ┌─────────────┼──────────────┐
              ▼             ▼              ▼
       ┌────────────┐ ┌──────────┐  ┌──────────┐
       │ PostgreSQL │ │   S3     │  │  Hosts   │
       │ (in/ext)   │ │ (in/ext) │  │ (agents) │
       └────────────┘ └──────────┘  └──────────┘
```

The frontend is the only externally-exposed service. It proxies `/api/v1/*` requests to the backend via Next.js server-side rewrites. The backend Service is ClusterIP only — not directly accessible from outside the cluster.

## Teardown

Remove everything (namespace + OAuthClient):

```bash
ansible-playbook deploy/ansible/undeploy.yaml
# Or with a custom namespace:
ansible-playbook deploy/ansible/undeploy.yaml -e troshka_namespace=troshka
```

This deletes the entire namespace (all pods, PVCs, secrets) and the OAuthClient CR.

**Warning:** This permanently deletes all data including the PostgreSQL database and MinIO storage. Back up any important data before running undeploy.

## Kustomize (Alternative)

If you prefer raw manifests over Ansible, use kustomize directly:

```bash
# Base only (you must edit backend-secret.yaml with real values)
oc apply -k deploy/base/

# With SSO overlay (edit oauth-client.yaml and oauth-proxy-secret.yaml)
oc apply -k deploy/overlays/sso/

# With PostgreSQL overlay
oc apply -k deploy/overlays/postgres/

# With MinIO overlay
oc apply -k deploy/overlays/minio/
```

Note: kustomize overlays require manual secret management. The Ansible playbook is recommended for production use.

## Upgrading

To upgrade to a newer version:

1. Update the image tags:
   ```bash
   ansible-playbook deploy/ansible/deploy.yaml \
     -e troshka_backend_image=quay.io/redhat-gpte/troshka-backend:v1.2.0 \
     -e troshka_frontend_image=quay.io/redhat-gpte/troshka-frontend:v1.2.0 \
     ... (same vars as original deploy)
   ```

2. The playbook will:
   - Update the Deployment images (triggers rolling restart)
   - Run a new Alembic migration Job (applies any schema changes)
   - Leave secrets and PVCs untouched

## Troubleshooting

### Pods Not Starting

```bash
oc get pods -n troshka
oc describe pod <pod-name> -n troshka
oc logs <pod-name> -n troshka
```

Common issues:
- **ImagePullBackOff** — check image name/tag, Quay.io credentials
- **CrashLoopBackOff on backend** — usually a DB connection issue; check `troshka-secrets` database-url
- **Pending PVC** — no storage class available; check `oc get sc`

### OAuth Proxy Not Working

```bash
oc get oauthclient troshka-troshka
oc logs deployment/troshka-oauth-proxy -n troshka
```

Common issues:
- **redirect_uri_mismatch** — route host doesn't match OAuthClient redirectURIs
- **TLS cert errors** — check `troshka-oauth-tls` secret was auto-created by the service-cert annotation

### Database Migration Failed

```bash
oc logs job/troshka-migrate-<timestamp> -n troshka
```

If the migration Job failed, check the logs for the specific error. Common causes:
- Database not reachable (PostgreSQL pod not ready)
- Schema conflicts (manual DB changes)

## Next Steps

After deploying Troshka, configure a host provider to start building environments:

- [AWS Provider Setup](install-aws.md)
- [GCP Provider Setup](install-gcp.md)
- [Azure Provider Setup](install-azure.md)
- [OCP Virt Provider Setup](install-ocpvirt.md)
