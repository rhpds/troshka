# Troshka Dedicated Instance — Catalog Item Design

## Overview

A Babylon catalog item that provisions a fully self-contained Troshka instance for a single user on any CNV-enabled OpenShift cluster. The instance includes its own PostgreSQL database, MinIO object storage, SSO integration, and a pre-configured agent host with seeded library images — ready to build demos immediately after provisioning.

## Architecture

```
┌─────────────────────── User's Namespace ───────────────────────┐
│                                                                 │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌────────────┐  │
│  │ OAuth    │──▶│ Frontend │──▶│ Backend  │──▶│ PostgreSQL │  │
│  │ Proxy    │   │ (Next.js)│   │ (FastAPI)│   │ (StatefulSet│  │
│  │ (SA-based│   └──────────┘   └────┬─────┘   └────────────┘  │
│  │  Route)  │                       │                           │
│  └──────────┘                       ▼                           │
│                                ┌──────────┐                     │
│                                │  MinIO   │                     │
│                                │ (S3-compat│                    │
│                                │  50Gi PVC)│                    │
│                                └──────────┘                     │
│                                     ▲                           │
│  ┌──────────────────────────────────┼───────────────────────┐  │
│  │ KubeVirt VM (Agent Host)         │                       │  │
│  │ ┌──────────┐                     │                       │  │
│  │ │ troshkad │─── S3 ops ──────────┘                       │  │
│  │ │ :31337   │                                             │  │
│  │ │          │─── nested VMs (libvirt/QEMU)                │  │
│  │ └──────────┘                                             │  │
│  │ vCPUs: 16 (default) | RAM: 64GB | Disk: 200GB           │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ServiceAccount: troshka-sa                                     │
│  RBAC: KubeVirt VMs, Services, PVCs, Routes                    │
└─────────────────────────────────────────────────────────────────┘

Central MinIO (ocpv-infra01)
  └── troshka-gold-images bucket
      ├── rhel-9.6-x86_64-kvm.qcow2
      └── rhel-9.6-x86_64-boot.iso
```

## Work Streams

### Work Stream 1: MinIO/S3-Compatible Storage — Backend Changes

The backend's `s3_storage.py` already supports `endpoint_url` for S3-compatible endpoints via boto3. The gap is on the troshkad (host agent) side: it uses the AWS CLI for uploads/downloads but doesn't receive or set the custom endpoint URL.

#### Changes Required

**`src/backend/app/services/pattern_service.py`** — Include `endpoint_url` in S3 credentials dict passed to troshkad capture/upload jobs.

**`src/backend/app/services/snapshot_service.py`** — Same: include `endpoint_url` in creds sent to troshkad.

**`src/backend/app/api/library.py`** — Same for library import jobs that go through troshkad.

**`src/troshkad/troshkad.py`** — In `_s3_upload()` and `_s3_download()`, read `endpoint_url` from credentials and set `AWS_ENDPOINT_URL` in the subprocess environment. The AWS CLI respects this env var natively.

**Presigned URL proxy** — When MinIO is behind a cluster-internal service (e.g., `http://troshka-minio:9000`), presigned URLs are not browser-reachable. Add a streaming upload proxy endpoint to the backend that pipes multipart uploads to MinIO. For a single-user dedicated instance, this is sufficient — no need to expose MinIO via a Route.

#### Estimated Impact

~30-40 lines of code changes for endpoint_url passthrough + a new proxy upload endpoint (~80-100 lines).

### Work Stream 2: Allowed Users — Backend Auth Change

Add `allowed_users` enforcement to the Troshka backend auth layer, following the pattern established in Parsec and Demolition.

#### Changes Required

**`src/backend/config/config.yaml`** — Add `allowed_users: ""` under `auth:` section.

**`src/backend/app/core/auth.py`** — In `get_current_user()`, after SSO user upsert, check if `allowed_users` is configured. If set and the authenticated identity (email or username from `X-Forwarded-Email` / `X-Forwarded-User`) is not in the list, return HTTP 403 Forbidden.

#### Design Notes

- OpenShift OAuth may return a username rather than an email in `X-Forwarded-Email` — the check must handle both (same as Parsec/Demolition).
- `allowed_users` is a CSV string, same format as `admin_users` and `operator_users`.
- When `allowed_users` is empty (default), all authenticated users are allowed (backward compatible).
- For the dedicated instance, both `allowed_users` and `admin_users` are set to the requester's identity.

### Work Stream 3: Central Image Repository

A shared MinIO instance on `ocpv-infra01.dal12.infra.demo.redhat.com` serving as the gold image source for all dedicated Troshka instances.

#### Setup

- Namespace: `troshka-images` (or similar)
- MinIO Deployment with persistent storage
- Single bucket: `troshka-gold-images`
- Exposed via OCP Route for cross-cluster access
- Credentials stored in a vault / agnosticv secret

#### Gold Images

Initial seed set:

| Name | Type | Format | Central Key |
|------|------|--------|-------------|
| Prebuilt RHEL 10.2 Bastion | image | qcow2 | `prebuilt-rhel-10.2-bastion.qcow2` |
| RHEL 10.2 KVM Guest Image | image | qcow2 | `rhel-10.2-x86_64-kvm.qcow2` |
| RHEL 10.2 Binary DVD | iso | iso | `rhel-10.2-x86_64-dvd.iso` |

The Prebuilt Bastion is tagged `ocp_default_image` and the Binary DVD is tagged `ocp_default_iso` so OCP templates work out of the box. The raw KVM Guest Image is included as a general-purpose RHEL disk without a default tag.

#### Maintenance

Admin uploads new images to the central MinIO manually. The catalog item references them by key in `troshka_seed_images` agnosticv variable. Updating gold images doesn't require workload role changes — just update agnosticv vars.

### Work Stream 4: AgnosticD Catalog Item

#### Config & Provider

- **Config**: `namespace`
- **Cloud provider**: `none`
- **Workload role**: `ocp4_workload_troshka_dedicated`

The `namespace` config handles cluster authentication (injecting `K8S_AUTH_*` env vars) and runs the workload list. All provisioning logic lives in the single workload role.

#### Agnosticv Catalog Item

Location: new directory in agnosticv (e.g., `agd_v2/troshka-dedicated/common.yaml`)

##### User-Facing Parameters

```yaml
__meta__:
  catalog:
    parameters:
    - name: troshka_host_vcpus
      description: Number of vCPUs for the Troshka agent host VM
      formLabel: Host vCPUs
      openAPIV3Schema:
        type: integer
        default: 16
        enum: [8, 16, 32, 64]
    - name: troshka_host_memory_gb
      description: Memory (GB) for the Troshka agent host VM
      formLabel: Host Memory (GB)
      openAPIV3Schema:
        type: integer
        default: 64
        enum: [32, 64, 128, 256]
    - name: troshka_host_disk_gb
      description: Data disk size (GB) for VM images and pattern cache
      formLabel: Host Disk (GB)
      openAPIV3Schema:
        type: integer
        default: 200
        enum: [100, 200, 500]
```

##### Key Variables

```yaml
config: namespace
cloud_provider: none

# Images
troshka_backend_image: "quay.io/redhat-gpte/troshka-backend:latest"
troshka_frontend_image: "quay.io/redhat-gpte/troshka-frontend:latest"

# Central image repo
troshka_seed_minio_endpoint: "https://minio-troshka-images.apps.ocpv-infra01.dal12.infra.demo.redhat.com"
troshka_seed_minio_bucket: "troshka-gold-images"
troshka_seed_minio_access_key: "{{ seed_minio_access_key }}"
troshka_seed_minio_secret_key: "{{ seed_minio_secret_key }}"
troshka_seed_images:
  - name: "Prebuilt RHEL 10.2 Bastion"
    type: image
    format: qcow2
    s3_object: "prebuilt-rhel-10.2-bastion.qcow2"
    tags: ["ocp_default_image"]
  - name: "RHEL 10.2 KVM Guest Image"
    type: image
    format: qcow2
    s3_object: "rhel-10.2-x86_64-kvm.qcow2"
    tags: []
  - name: "RHEL 10.2 Binary DVD"
    type: iso
    format: iso
    s3_object: "rhel-10.2-x86_64-dvd.iso"
    tags: ["ocp_default_iso"]

# Workloads
workloads:
  - ocp4_workload_troshka_dedicated
```

#### Workload Role — Provisioning Sequence

The role `ocp4_workload_troshka_dedicated` executes these task files in order:

##### 1. `tasks/serviceaccount.yaml`
- Create ServiceAccount `troshka-sa`
- Create Role with permissions: KubeVirt VMs/VMIs, CDI DataVolumes, Services, PVCs, Routes, Secrets
- Create RoleBinding
- Annotate SA for OAuth redirect: `serviceaccounts.openshift.io/oauth-redirecturi.troshka: https://<route-host>/oauth/callback`

##### 2. `tasks/postgres.yaml`
- PostgreSQL 16 StatefulSet (1 replica, 10Gi PVC)
- Headless Service for database access
- Generate and store DB password in Secret

##### 3. `tasks/minio.yaml`
- MinIO Deployment (1 replica, 50Gi PVC)
- Service on ports 9000 (S3 API) + 9001 (console)
- Generate and store MinIO root credentials in Secret
- Create `troshka-images` bucket (via mc CLI init container or post-start job)

##### 4. `tasks/secrets.yaml`
- Generate JWT secret, Fernet encryption key
- Store all secrets in `troshka-secrets` Secret

##### 5. `tasks/backend.yaml`
- Backend Deployment + ClusterIP Service (port 8200)
- ConfigMap with:
  - `database.url` pointing to in-namespace PostgreSQL
  - `s3.endpoint_url` pointing to in-namespace MinIO
  - `s3.bucket: troshka-images`
  - `auth.oauth_enabled: true`
  - `auth.admin_users: <requester_identity>`
  - `auth.allowed_users: <requester_identity>`
- Environment variables from Secret (JWT, encryption key, S3 creds, DB password)

##### 6. `tasks/migrate.yaml`
- Kubernetes Job running `alembic upgrade head`
- `ttlSecondsAfterFinished: 600`

##### 7. `tasks/frontend.yaml`
- Frontend Deployment + ClusterIP Service (port 3000)
- `BACKEND_URL` env var pointing to backend service

##### 8. `tasks/oauth.yaml`
- OAuth proxy Deployment (port 8443):
  - `--provider=openshift`
  - `--client-id=system:serviceaccount:<namespace>:troshka-sa`
  - `--client-secret-file=/var/run/secrets/kubernetes.io/serviceaccount/token`
  - `--pass-user-headers=true`
  - `--upstream=http://troshka-frontend:3000`
- Route with edge TLS termination pointing to oauth-proxy

##### 9. `tasks/provider.yaml`
- Wait for backend readiness (`/api/v1/health`)
- Obtain dev token or use API with SSO headers
- Call `POST /providers/` to create OCP Virt provider:
  - `type: ocpvirt`
  - `api_url: <cluster API URL from sandbox vars>`
  - `credentials: { token: <SA token>, namespace: <namespace> }`

##### 10. `tasks/host.yaml`
- Call `POST /hosts/` to create a host:
  - `provider_id: <from step 9>`
  - `vcpus: {{ troshka_host_vcpus }}`
  - `memory_gb: {{ troshka_host_memory_gb }}`
  - `disk_gb: {{ troshka_host_disk_gb }}`
- Wait for host to reach `connected` state (troshkad installed and registered)
- Timeout: 10 minutes

##### 11. `tasks/seed.yaml`
- For each image in `troshka_seed_images`:
  - Copy from central MinIO to user's MinIO:
    - Source: `s3://<central_bucket>/<s3_object>` (using central MinIO endpoint + creds)
    - Dest: `s3://troshka-images/library/<user_id>/<item_id>/<name>.<format>` (using local MinIO endpoint + creds)
  - Call Troshka API `POST /library/` to create library item with `s3_key`, `size_bytes`, `state: "ready"`
  - Apply tags (`ocp_default_image`, `ocp_default_iso`) via `PATCH /library/<item_id>`
- Implementation: temporary Pod with `aws` CLI configured for both endpoints, or `mc` (MinIO client) mirror

#### Destroy Sequence

Defined in `remove_workloads` or the role's `tasks/remove_workload.yaml`:

1. Call Troshka API to destroy all active projects
2. Call Troshka API to delete all hosts (Troshka deletes KubeVirt VMs)
3. Delete namespace (cascading delete removes all K8s resources + PVCs)

No cluster-scoped resources to clean up — SA-based OAuth requires no `OAuthClient` CR.

The `namespace` config's `destroy_env.yml` handles safety checks (DNS resolution, token validation) before attempting destroy.

## Provisioning Timeline

| Step | Duration | Notes |
|------|----------|-------|
| SA + RBAC | ~5s | |
| PostgreSQL | ~30s | PVC provisioning + pod ready |
| MinIO | ~30s | PVC provisioning + pod ready + bucket creation |
| Secrets + Backend + Migration | ~60s | Backend startup + schema migration |
| Frontend + OAuth + Route | ~30s | |
| Register provider | ~5s | API call |
| Create host (KubeVirt VM) | ~5-8 min | VM provisioning + boot + troshkad install |
| Seed library | ~2-5 min | Image copy depends on size and network |
| **Total** | **~10-15 min** | |

## User Experience

After provisioning completes, the user receives a Route URL. They:

1. Click the URL → redirected to OpenShift login
2. Authenticate with cluster credentials → redirected back to Troshka
3. Land on Projects page with library pre-loaded (RHEL bastion image + install ISO)
4. Can immediately create projects, import templates, deploy OCP clusters
5. Can add more hosts, upload additional images, save patterns — full self-service

Only their identity has access. No other cluster users can reach the instance.

## Scope Boundaries

**In scope:**
- MinIO endpoint_url passthrough to troshkad
- Upload proxy endpoint for MinIO (browser can't reach cluster-internal MinIO)
- `allowed_users` auth enforcement in backend
- Central MinIO setup on infra01
- Workload role for agnosticD namespace config
- Agnosticv catalog item with configurable host parameters
- Library seeding from central repo

**Out of scope:**
- Route-based MinIO exposure (proxy is sufficient for single-user)
- Group-based access control (email/username list is sufficient)
- Multi-user shared instances
- Automatic image updates (admin manually uploads to central MinIO)
- Shared storage pools / live migration (single host, local storage only)
