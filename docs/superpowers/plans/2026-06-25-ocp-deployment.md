# OCP Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Troshka backend and frontend deployable on OpenShift from container images on quay.io/rhpds/, with Ansible playbook for installation.

**Architecture:** Two container images (backend=FastAPI/uvicorn, frontend=Next.js standalone). Kustomize base manifests with overlays for SSO, PostgreSQL, MinIO. Ansible playbook as primary install method using kubernetes.core.k8s with Jinja2 templates. GitHub Actions CI builds and pushes images.

**Tech Stack:** Podman/Buildah (container builds), Kustomize (manifests), Ansible + kubernetes.core (playbook), GitHub Actions (CI), ose-oauth-proxy-rhel9 (SSO)

## Global Constraints

- Base images: UBI9 only (`registry.access.redhat.com/ubi9/*`)
- Python 3.11+, Node.js 18+
- Backend port: 8200, frontend port: 3000 (Next.js standalone default)
- Config: Dynaconf with `TROSHKA_*` env var prefix (already works)
- Alembic reads DB URL via Dynaconf (`app.core.config`), not from `alembic.ini`
- All Ansible tasks use `kubernetes.core.k8s` with inline YAML definitions
- Containerfiles (not Dockerfiles) — Red Hat convention
- No `libvirt-python` in container image — it requires system libvirt-devel headers. The backend container never talks to libvirt directly (that's troshkad on hosts). Strip it from the container build's requirements.

---

### Task 1: Containerfiles

Build the two container images. Validate they build and run locally.

**Files:**
- Create: `deploy/containerfiles/Containerfile.backend`
- Create: `deploy/containerfiles/Containerfile.frontend`
- Create: `deploy/containerfiles/.dockerignore`
- Modify: `src/frontend/next.config.ts`

**Interfaces:**
- Produces: two buildable container images (`troshka-backend`, `troshka-frontend`)

- [ ] **Step 1: Create backend Containerfile**

Create `deploy/containerfiles/Containerfile.backend`:

```dockerfile
FROM registry.access.redhat.com/ubi9/python-311:latest

WORKDIR /opt/app-root/src

COPY src/backend/requirements.txt .
# libvirt-python needs system headers not available in container — exclude it
RUN grep -v '^libvirt-python' requirements.txt > requirements-container.txt && \
    pip install --no-cache-dir -r requirements-container.txt

COPY src/backend/ .
COPY src/troshkad/troshkad.py /opt/app-root/troshkad/troshkad.py

EXPOSE 8200

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8200"]
```

- [ ] **Step 2: Create frontend Containerfile**

Create `deploy/containerfiles/Containerfile.frontend`:

```dockerfile
FROM registry.access.redhat.com/ubi9/nodejs-18:latest AS builder

WORKDIR /opt/app-root/src

COPY src/frontend/package.json src/frontend/package-lock.json ./
RUN npm ci

COPY src/frontend/ .

ENV STANDALONE=true
RUN npm run build

FROM registry.access.redhat.com/ubi9/nodejs-18-minimal:latest

WORKDIR /opt/app-root/src

COPY --from=builder /opt/app-root/src/.next/standalone ./
COPY --from=builder /opt/app-root/src/.next/static ./.next/static
COPY --from=builder /opt/app-root/src/public ./public

ENV NODE_ENV=production
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

EXPOSE 3000

CMD ["node", "server.js"]
```

- [ ] **Step 3: Create .dockerignore**

Create `deploy/containerfiles/.dockerignore`:

```
**/.git
**/.next
**/node_modules
**/__pycache__
**/venv
**/.venv
**/*.pyc
**/.DS_Store
**/.superpowers
**/.playwright-mcp
**/.claude
**/shanotes.txt
```

- [ ] **Step 4: Modify next.config.ts for conditional standalone output**

Modify `src/frontend/next.config.ts` — add conditional `output: 'standalone'`:

```typescript
import type { NextConfig } from "next";

const backendUrl = process.env.BACKEND_URL || "http://localhost:8200";

const nextConfig: NextConfig = {
  ...(process.env.STANDALONE === "true" && { output: "standalone" }),
  async rewrites() {
    return [
      {
        source: "/api/v1/:path*",
        destination: `${backendUrl}/api/v1/:path*`,
      },
      {
        source: "/ws/:path*",
        destination: `${backendUrl}/ws/:path*`,
      },
    ];
  },
};

export default nextConfig;
```

- [ ] **Step 5: Test backend image build**

Build from project root (the Containerfiles use paths relative to repo root):

```bash
podman build -f deploy/containerfiles/Containerfile.backend -t troshka-backend:dev .
```

Expected: image builds successfully. Verify:

```bash
podman run --rm troshka-backend:dev python3 -c "from app.main import app; print('OK')"
```

Expected: prints `OK`

- [ ] **Step 6: Test frontend image build**

```bash
podman build -f deploy/containerfiles/Containerfile.frontend -t troshka-frontend:dev .
```

Expected: image builds. Verify standalone output exists:

```bash
podman run --rm troshka-frontend:dev ls -la server.js
```

Expected: `server.js` file exists

- [ ] **Step 7: Commit**

```bash
git add deploy/containerfiles/ src/frontend/next.config.ts
git commit -m "feat: add Containerfiles for backend and frontend OCP deployment"
```

---

### Task 2: Kustomize Base Manifests

Create the base Kubernetes manifests for backend, frontend, and supporting resources.

**Files:**
- Create: `deploy/base/kustomization.yaml`
- Create: `deploy/base/namespace.yaml`
- Create: `deploy/base/backend-deployment.yaml`
- Create: `deploy/base/frontend-deployment.yaml`
- Create: `deploy/base/frontend-route.yaml`
- Create: `deploy/base/backend-config.yaml`
- Create: `deploy/base/backend-secret.yaml`

**Interfaces:**
- Consumes: container images from Task 1
- Produces: deployable kustomize base (`oc apply -k deploy/base`)

- [ ] **Step 1: Create namespace.yaml**

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: troshka
```

- [ ] **Step 2: Create backend-secret.yaml**

Placeholder Secret — values must be overridden by user or Ansible:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: troshka-secrets
  namespace: troshka
type: Opaque
stringData:
  database-url: "postgresql+psycopg2://troshka:CHANGE_ME@troshka-postgres:5432/troshka"
  jwt-secret: "CHANGE_ME"
  encryption-key: "CHANGE_ME"
  s3-access-key: ""
  s3-secret-key: ""
```

- [ ] **Step 3: Create backend-config.yaml**

ConfigMap with non-sensitive config:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: troshka-config
  namespace: troshka
data:
  config.yaml: |
    app:
      port: 8200
      host: "0.0.0.0"
      external_url: ""
    auth:
      oauth_enabled: false
      admin_users: ""
      operator_users: ""
    defaults:
      run_timer_hours: 8
      lifetime_days: 30
      max_vms_per_project: 20
      max_projects_per_user: 10
      user_library_quota_gb: 500
    overcommit:
      cpu_ratio: 4.0
      ram_ratio: 1.5
```

- [ ] **Step 4: Create backend-deployment.yaml**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: troshka-backend
  namespace: troshka
  labels:
    app.kubernetes.io/name: troshka-backend
    app.kubernetes.io/component: backend
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: troshka-backend
  template:
    metadata:
      labels:
        app.kubernetes.io/name: troshka-backend
        app.kubernetes.io/component: backend
    spec:
      containers:
        - name: backend
          image: quay.io/rhpds/troshka-backend:latest
          ports:
            - containerPort: 8200
              name: http
          env:
            - name: TROSHKA_DATABASE__URL
              valueFrom:
                secretKeyRef:
                  name: troshka-secrets
                  key: database-url
            - name: TROSHKA_AUTH__JWT_SECRET
              valueFrom:
                secretKeyRef:
                  name: troshka-secrets
                  key: jwt-secret
            - name: TROSHKA_AUTH__ENCRYPTION_KEY
              valueFrom:
                secretKeyRef:
                  name: troshka-secrets
                  key: encryption-key
            - name: TROSHKA_AWS__ACCESS_KEY_ID
              valueFrom:
                secretKeyRef:
                  name: troshka-secrets
                  key: s3-access-key
                  optional: true
            - name: TROSHKA_AWS__SECRET_ACCESS_KEY
              valueFrom:
                secretKeyRef:
                  name: troshka-secrets
                  key: s3-secret-key
                  optional: true
          volumeMounts:
            - name: config
              mountPath: /opt/app-root/src/config/config.yaml
              subPath: config.yaml
          readinessProbe:
            httpGet:
              path: /api/v1/health
              port: 8200
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /api/v1/health
              port: 8200
            initialDelaySeconds: 15
            periodSeconds: 30
          resources:
            requests:
              cpu: 250m
              memory: 512Mi
            limits:
              memory: 2Gi
      volumes:
        - name: config
          configMap:
            name: troshka-config
---
apiVersion: v1
kind: Service
metadata:
  name: troshka-backend
  namespace: troshka
  labels:
    app.kubernetes.io/name: troshka-backend
spec:
  selector:
    app.kubernetes.io/name: troshka-backend
  ports:
    - port: 8200
      targetPort: 8200
      name: http
```

- [ ] **Step 5: Create frontend-deployment.yaml**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: troshka-frontend
  namespace: troshka
  labels:
    app.kubernetes.io/name: troshka-frontend
    app.kubernetes.io/component: frontend
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: troshka-frontend
  template:
    metadata:
      labels:
        app.kubernetes.io/name: troshka-frontend
        app.kubernetes.io/component: frontend
    spec:
      containers:
        - name: frontend
          image: quay.io/rhpds/troshka-frontend:latest
          ports:
            - containerPort: 3000
              name: http
          env:
            - name: BACKEND_URL
              value: "http://troshka-backend:8200"
          readinessProbe:
            httpGet:
              path: /
              port: 3000
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              memory: 512Mi
---
apiVersion: v1
kind: Service
metadata:
  name: troshka-frontend
  namespace: troshka
  labels:
    app.kubernetes.io/name: troshka-frontend
spec:
  selector:
    app.kubernetes.io/name: troshka-frontend
  ports:
    - port: 3000
      targetPort: 3000
      name: http
```

- [ ] **Step 6: Create frontend-route.yaml**

```yaml
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: troshka
  namespace: troshka
  labels:
    app.kubernetes.io/name: troshka
spec:
  tls:
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
  to:
    kind: Service
    name: troshka-frontend
    weight: 100
  port:
    targetPort: http
```

- [ ] **Step 7: Create kustomization.yaml**

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

namespace: troshka

resources:
  - namespace.yaml
  - backend-config.yaml
  - backend-secret.yaml
  - backend-deployment.yaml
  - frontend-deployment.yaml
  - frontend-route.yaml
```

- [ ] **Step 8: Validate kustomize build**

```bash
oc kustomize deploy/base/
```

Expected: valid combined YAML output with all resources in `troshka` namespace.

- [ ] **Step 9: Commit**

```bash
git add deploy/base/
git commit -m "feat: add kustomize base manifests for OCP deployment"
```

---

### Task 3: Kustomize Overlays (SSO, PostgreSQL, MinIO)

Add the three optional overlays.

**Files:**
- Create: `deploy/overlays/sso/kustomization.yaml`
- Create: `deploy/overlays/sso/oauth-proxy-deployment.yaml`
- Create: `deploy/overlays/sso/oauth-proxy-secret.yaml`
- Create: `deploy/overlays/sso/oauth-client.yaml`
- Create: `deploy/overlays/sso/oauth-serviceaccount.yaml`
- Create: `deploy/overlays/sso/frontend-route-patch.yaml`
- Create: `deploy/overlays/postgres/kustomization.yaml`
- Create: `deploy/overlays/postgres/postgres-statefulset.yaml`
- Create: `deploy/overlays/postgres/postgres-service.yaml`
- Create: `deploy/overlays/postgres/backend-secret-patch.yaml`
- Create: `deploy/overlays/minio/kustomization.yaml`
- Create: `deploy/overlays/minio/minio-deployment.yaml`
- Create: `deploy/overlays/minio/minio-service.yaml`
- Create: `deploy/overlays/minio/backend-config-patch.yaml`

**Interfaces:**
- Consumes: base manifests from Task 2
- Produces: three overlay directories, each deployable with `oc apply -k`

- [ ] **Step 1: Create SSO overlay — oauth-serviceaccount.yaml**

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: troshka-oauth
  namespace: troshka
  annotations:
    serviceaccounts.openshift.io/oauth-redirectreference.primary: >-
      {"kind":"OAuthRedirectReference","apiVersion":"v1","reference":{"kind":"Route","name":"troshka"}}
```

- [ ] **Step 2: Create SSO overlay — oauth-proxy-secret.yaml**

Placeholder — Ansible generates real values:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: troshka-oauth-proxy
  namespace: troshka
type: Opaque
stringData:
  client-secret: "CHANGE_ME"
  cookie-secret: "CHANGE_ME"
```

- [ ] **Step 3: Create SSO overlay — oauth-client.yaml**

```yaml
apiVersion: oauth.openshift.io/v1
kind: OAuthClient
metadata:
  name: troshka
grantMethod: auto
secret: "CHANGE_ME"
redirectURIs:
  - "https://CHANGE_ME/oauth/callback"
```

- [ ] **Step 4: Create SSO overlay — oauth-proxy-deployment.yaml**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: troshka-oauth-proxy
  namespace: troshka
  labels:
    app.kubernetes.io/name: troshka-oauth-proxy
    app.kubernetes.io/component: auth
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: troshka-oauth-proxy
  template:
    metadata:
      labels:
        app.kubernetes.io/name: troshka-oauth-proxy
        app.kubernetes.io/component: auth
    spec:
      serviceAccountName: troshka-oauth
      containers:
        - name: oauth-proxy
          image: registry.redhat.io/openshift4/ose-oauth-proxy-rhel9:latest
          ports:
            - containerPort: 8443
              name: proxy
          args:
            - --provider=openshift
            - --https-address=:8443
            - --http-address=
            - --upstream=http://troshka-frontend:3000
            - --tls-cert=/etc/tls/private/tls.crt
            - --tls-key=/etc/tls/private/tls.key
            - --cookie-secret-file=/etc/oauth/cookie-secret
            - --client-id=troshka
            - --client-secret-file=/etc/oauth/client-secret
            - --openshift-service-account=troshka-oauth
            - --openshift-ca=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
            - --pass-user-headers=true
            - --pass-access-token=false
            - --skip-auth-regex=^/api/v1/health
          volumeMounts:
            - name: tls
              mountPath: /etc/tls/private
            - name: oauth-secrets
              mountPath: /etc/oauth
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              memory: 128Mi
      volumes:
        - name: tls
          secret:
            secretName: troshka-oauth-tls
        - name: oauth-secrets
          secret:
            secretName: troshka-oauth-proxy
---
apiVersion: v1
kind: Service
metadata:
  name: troshka-oauth-proxy
  namespace: troshka
  annotations:
    service.beta.openshift.io/serving-cert-secret-name: troshka-oauth-tls
  labels:
    app.kubernetes.io/name: troshka-oauth-proxy
spec:
  selector:
    app.kubernetes.io/name: troshka-oauth-proxy
  ports:
    - port: 8443
      targetPort: 8443
      name: proxy
```

- [ ] **Step 5: Create SSO overlay — frontend-route-patch.yaml**

Patches the Route to point to oauth-proxy instead of frontend:

```yaml
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: troshka
  namespace: troshka
spec:
  tls:
    termination: reencrypt
  to:
    kind: Service
    name: troshka-oauth-proxy
  port:
    targetPort: proxy
```

- [ ] **Step 6: Create SSO overlay — kustomization.yaml**

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - ../../base
  - oauth-serviceaccount.yaml
  - oauth-proxy-secret.yaml
  - oauth-client.yaml
  - oauth-proxy-deployment.yaml

patchesStrategicMerge:
  - frontend-route-patch.yaml
```

- [ ] **Step 7: Create PostgreSQL overlay — postgres-statefulset.yaml + postgres-service.yaml**

`postgres-statefulset.yaml`:

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: troshka-postgres
  namespace: troshka
  labels:
    app.kubernetes.io/name: troshka-postgres
    app.kubernetes.io/component: database
spec:
  serviceName: troshka-postgres
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: troshka-postgres
  template:
    metadata:
      labels:
        app.kubernetes.io/name: troshka-postgres
        app.kubernetes.io/component: database
    spec:
      containers:
        - name: postgres
          image: registry.redhat.io/rhel9/postgresql-16:latest
          ports:
            - containerPort: 5432
          env:
            - name: POSTGRESQL_USER
              value: troshka
            - name: POSTGRESQL_PASSWORD
              value: troshka
            - name: POSTGRESQL_DATABASE
              value: troshka
          volumeMounts:
            - name: data
              mountPath: /var/lib/pgsql/data
          readinessProbe:
            exec:
              command: ["pg_isready", "-U", "troshka", "-d", "troshka"]
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              memory: 1Gi
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: ["ReadWriteOnce"]
        resources:
          requests:
            storage: 10Gi
```

`postgres-service.yaml`:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: troshka-postgres
  namespace: troshka
  labels:
    app.kubernetes.io/name: troshka-postgres
spec:
  selector:
    app.kubernetes.io/name: troshka-postgres
  ports:
    - port: 5432
      targetPort: 5432
  clusterIP: None
```

- [ ] **Step 8: Create PostgreSQL overlay — backend-secret-patch.yaml + kustomization.yaml**

`backend-secret-patch.yaml`:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: troshka-secrets
  namespace: troshka
stringData:
  database-url: "postgresql+psycopg2://troshka:troshka@troshka-postgres:5432/troshka"
```

`kustomization.yaml`:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - ../../base
  - postgres-statefulset.yaml
  - postgres-service.yaml

patchesStrategicMerge:
  - backend-secret-patch.yaml
```

- [ ] **Step 9: Create MinIO overlay — minio-deployment.yaml + minio-service.yaml**

`minio-deployment.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: troshka-minio
  namespace: troshka
  labels:
    app.kubernetes.io/name: troshka-minio
    app.kubernetes.io/component: storage
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: troshka-minio
  template:
    metadata:
      labels:
        app.kubernetes.io/name: troshka-minio
        app.kubernetes.io/component: storage
    spec:
      containers:
        - name: minio
          image: quay.io/minio/minio:latest
          args: ["server", "/data", "--console-address", ":9001"]
          ports:
            - containerPort: 9000
              name: s3
            - containerPort: 9001
              name: console
          env:
            - name: MINIO_ROOT_USER
              value: minioadmin
            - name: MINIO_ROOT_PASSWORD
              value: minioadmin
          volumeMounts:
            - name: data
              mountPath: /data
          readinessProbe:
            httpGet:
              path: /minio/health/ready
              port: 9000
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              memory: 1Gi
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: troshka-minio-data
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: troshka-minio-data
  namespace: troshka
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 50Gi
```

`minio-service.yaml`:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: troshka-minio
  namespace: troshka
  labels:
    app.kubernetes.io/name: troshka-minio
spec:
  selector:
    app.kubernetes.io/name: troshka-minio
  ports:
    - port: 9000
      targetPort: 9000
      name: s3
    - port: 9001
      targetPort: 9001
      name: console
```

- [ ] **Step 10: Create MinIO overlay — backend-config-patch.yaml + kustomization.yaml**

`backend-config-patch.yaml`:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: troshka-config
  namespace: troshka
data:
  config.yaml: |
    app:
      port: 8200
      host: "0.0.0.0"
      external_url: ""
    s3:
      endpoint_url: "http://troshka-minio:9000"
      bucket: "troshka-images"
    auth:
      oauth_enabled: false
      admin_users: ""
      operator_users: ""
    defaults:
      run_timer_hours: 8
      lifetime_days: 30
      max_vms_per_project: 20
      max_projects_per_user: 10
      user_library_quota_gb: 500
    overcommit:
      cpu_ratio: 4.0
      ram_ratio: 1.5
```

`kustomization.yaml`:

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - ../../base
  - minio-deployment.yaml
  - minio-service.yaml

patchesStrategicMerge:
  - backend-config-patch.yaml
```

- [ ] **Step 11: Validate all overlays build**

```bash
oc kustomize deploy/overlays/sso/
oc kustomize deploy/overlays/postgres/
oc kustomize deploy/overlays/minio/
```

Expected: each produces valid combined YAML.

- [ ] **Step 12: Commit**

```bash
git add deploy/overlays/
git commit -m "feat: add kustomize overlays for SSO, PostgreSQL, and MinIO"
```

---

### Task 4: Ansible Playbook

Create the Ansible deploy/undeploy playbooks with Jinja2-templated Kubernetes resources.

**Files:**
- Create: `deploy/ansible/deploy.yaml`
- Create: `deploy/ansible/undeploy.yaml`
- Create: `deploy/ansible/inventory/group_vars/all.yaml`
- Create: `deploy/ansible/tasks/namespace.yaml`
- Create: `deploy/ansible/tasks/secrets.yaml`
- Create: `deploy/ansible/tasks/database.yaml`
- Create: `deploy/ansible/tasks/minio.yaml`
- Create: `deploy/ansible/tasks/oauth.yaml`
- Create: `deploy/ansible/tasks/backend.yaml`
- Create: `deploy/ansible/tasks/frontend.yaml`
- Create: `deploy/ansible/tasks/migrate.yaml`

**Interfaces:**
- Consumes: container images from Task 1, manifest patterns from Tasks 2-3
- Produces: `ansible-playbook deploy/ansible/deploy.yaml` installs Troshka on OCP

- [ ] **Step 1: Create default vars — `inventory/group_vars/all.yaml`**

```yaml
troshka_namespace: troshka

troshka_backend_image: quay.io/rhpds/troshka-backend:latest
troshka_frontend_image: quay.io/rhpds/troshka-frontend:latest

troshka_route_host: ""

# Database
troshka_db_url: ""
troshka_deploy_postgres: false
troshka_postgres_storage_size: 10Gi
troshka_postgres_image: "registry.redhat.io/rhel9/postgresql-16:latest"
troshka_postgres_password: troshka

# S3
troshka_s3_bucket: troshka-images
troshka_s3_access_key: ""
troshka_s3_secret_key: ""
troshka_s3_endpoint: ""
troshka_deploy_minio: false
troshka_minio_storage_size: 50Gi
troshka_minio_root_user: minioadmin
troshka_minio_root_password: minioadmin

# Auth
troshka_oauth_enabled: false
troshka_admin_users: ""
troshka_operator_users: ""

# Defaults
troshka_run_timer_hours: 8
troshka_lifetime_days: 30

# Resource limits
troshka_backend_memory_limit: 2Gi
troshka_backend_cpu_request: 250m
troshka_frontend_memory_limit: 512Mi
troshka_frontend_cpu_request: 100m
```

- [ ] **Step 2: Create main playbook — `deploy.yaml`**

```yaml
---
- name: Deploy Troshka on OpenShift
  hosts: localhost
  connection: local
  gather_facts: false

  tasks:
    - name: Create namespace
      ansible.builtin.include_tasks: tasks/namespace.yaml

    - name: Manage secrets
      ansible.builtin.include_tasks: tasks/secrets.yaml

    - name: Deploy PostgreSQL
      ansible.builtin.include_tasks: tasks/database.yaml
      when: troshka_deploy_postgres | bool

    - name: Deploy MinIO
      ansible.builtin.include_tasks: tasks/minio.yaml
      when: troshka_deploy_minio | bool

    - name: Deploy backend
      ansible.builtin.include_tasks: tasks/backend.yaml

    - name: Run database migration
      ansible.builtin.include_tasks: tasks/migrate.yaml

    - name: Deploy frontend
      ansible.builtin.include_tasks: tasks/frontend.yaml

    - name: Configure OAuth
      ansible.builtin.include_tasks: tasks/oauth.yaml
      when: troshka_oauth_enabled | bool

    - name: Display access info
      ansible.builtin.debug:
        msg: >-
          Troshka deployed to namespace '{{ troshka_namespace }}'.
          Route: https://{{ _troshka_route_host }}
```

- [ ] **Step 3: Create `tasks/namespace.yaml`**

```yaml
---
- name: Create namespace
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: Namespace
      metadata:
        name: "{{ troshka_namespace }}"
```

- [ ] **Step 4: Create `tasks/secrets.yaml`**

```yaml
---
- name: Check if secrets exist
  kubernetes.core.k8s_info:
    api_version: v1
    kind: Secret
    name: troshka-secrets
    namespace: "{{ troshka_namespace }}"
  register: _existing_secrets

- name: Generate secrets if not present
  when: _existing_secrets.resources | length == 0
  block:
    - name: Generate JWT secret
      ansible.builtin.set_fact:
        _jwt_secret: "{{ lookup('password', '/dev/null chars=ascii_letters,digits length=48') }}"

    - name: Generate encryption key
      ansible.builtin.set_fact:
        _encryption_key: "{{ lookup('password', '/dev/null chars=ascii_letters,digits length=44') }}"

    - name: Determine database URL
      ansible.builtin.set_fact:
        _db_url: >-
          {{ troshka_db_url | default('', true) |
             ternary(troshka_db_url,
                      'postgresql+psycopg2://troshka:' ~ troshka_postgres_password ~ '@troshka-postgres.' ~ troshka_namespace ~ '.svc:5432/troshka') }}

    - name: Create secrets
      kubernetes.core.k8s:
        state: present
        definition:
          apiVersion: v1
          kind: Secret
          metadata:
            name: troshka-secrets
            namespace: "{{ troshka_namespace }}"
          type: Opaque
          stringData:
            database-url: "{{ _db_url }}"
            jwt-secret: "{{ _jwt_secret }}"
            encryption-key: "{{ _encryption_key }}"
            s3-access-key: "{{ troshka_s3_access_key | default(troshka_minio_root_user, true) }}"
            s3-secret-key: "{{ troshka_s3_secret_key | default(troshka_minio_root_password, true) }}"

- name: Load existing secrets
  when: _existing_secrets.resources | length > 0
  ansible.builtin.set_fact:
    _db_url: "{{ _existing_secrets.resources[0].data['database-url'] | b64decode }}"
```

- [ ] **Step 5: Create `tasks/database.yaml`**

```yaml
---
- name: Deploy PostgreSQL StatefulSet
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: apps/v1
      kind: StatefulSet
      metadata:
        name: troshka-postgres
        namespace: "{{ troshka_namespace }}"
        labels:
          app.kubernetes.io/name: troshka-postgres
      spec:
        serviceName: troshka-postgres
        replicas: 1
        selector:
          matchLabels:
            app.kubernetes.io/name: troshka-postgres
        template:
          metadata:
            labels:
              app.kubernetes.io/name: troshka-postgres
          spec:
            containers:
              - name: postgres
                image: "{{ troshka_postgres_image }}"
                ports:
                  - containerPort: 5432
                env:
                  - name: POSTGRESQL_USER
                    value: troshka
                  - name: POSTGRESQL_PASSWORD
                    value: "{{ troshka_postgres_password }}"
                  - name: POSTGRESQL_DATABASE
                    value: troshka
                volumeMounts:
                  - name: data
                    mountPath: /var/lib/pgsql/data
                readinessProbe:
                  exec:
                    command: ["pg_isready", "-U", "troshka", "-d", "troshka"]
                  initialDelaySeconds: 5
                  periodSeconds: 10
                resources:
                  requests:
                    cpu: 100m
                    memory: 256Mi
                  limits:
                    memory: 1Gi
        volumeClaimTemplates:
          - metadata:
              name: data
            spec:
              accessModes: ["ReadWriteOnce"]
              resources:
                requests:
                  storage: "{{ troshka_postgres_storage_size }}"

- name: Create PostgreSQL Service
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: Service
      metadata:
        name: troshka-postgres
        namespace: "{{ troshka_namespace }}"
      spec:
        selector:
          app.kubernetes.io/name: troshka-postgres
        ports:
          - port: 5432
            targetPort: 5432
        clusterIP: None

- name: Wait for PostgreSQL to be ready
  kubernetes.core.k8s_info:
    api_version: apps/v1
    kind: StatefulSet
    name: troshka-postgres
    namespace: "{{ troshka_namespace }}"
  register: _pg_sts
  until: (_pg_sts.resources[0].status.readyReplicas | default(0)) >= 1
  retries: 30
  delay: 10
```

- [ ] **Step 6: Create `tasks/minio.yaml`**

```yaml
---
- name: Deploy MinIO PVC
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: PersistentVolumeClaim
      metadata:
        name: troshka-minio-data
        namespace: "{{ troshka_namespace }}"
      spec:
        accessModes: ["ReadWriteOnce"]
        resources:
          requests:
            storage: "{{ troshka_minio_storage_size }}"

- name: Deploy MinIO
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: troshka-minio
        namespace: "{{ troshka_namespace }}"
        labels:
          app.kubernetes.io/name: troshka-minio
      spec:
        replicas: 1
        selector:
          matchLabels:
            app.kubernetes.io/name: troshka-minio
        template:
          metadata:
            labels:
              app.kubernetes.io/name: troshka-minio
          spec:
            containers:
              - name: minio
                image: quay.io/minio/minio:latest
                args: ["server", "/data", "--console-address", ":9001"]
                ports:
                  - containerPort: 9000
                    name: s3
                  - containerPort: 9001
                    name: console
                env:
                  - name: MINIO_ROOT_USER
                    value: "{{ troshka_minio_root_user }}"
                  - name: MINIO_ROOT_PASSWORD
                    value: "{{ troshka_minio_root_password }}"
                volumeMounts:
                  - name: data
                    mountPath: /data
                readinessProbe:
                  httpGet:
                    path: /minio/health/ready
                    port: 9000
                  initialDelaySeconds: 5
                  periodSeconds: 10
                resources:
                  requests:
                    cpu: 100m
                    memory: 256Mi
                  limits:
                    memory: 1Gi
            volumes:
              - name: data
                persistentVolumeClaim:
                  claimName: troshka-minio-data

- name: Create MinIO Service
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: Service
      metadata:
        name: troshka-minio
        namespace: "{{ troshka_namespace }}"
      spec:
        selector:
          app.kubernetes.io/name: troshka-minio
        ports:
          - port: 9000
            targetPort: 9000
            name: s3
          - port: 9001
            targetPort: 9001
            name: console

- name: Wait for MinIO to be ready
  kubernetes.core.k8s_info:
    api_version: apps/v1
    kind: Deployment
    name: troshka-minio
    namespace: "{{ troshka_namespace }}"
  register: _minio_deploy
  until: (_minio_deploy.resources[0].status.readyReplicas | default(0)) >= 1
  retries: 20
  delay: 10

- name: Create default S3 bucket
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: batch/v1
      kind: Job
      metadata:
        name: "troshka-minio-init-{{ lookup('pipe', 'date +%s') }}"
        namespace: "{{ troshka_namespace }}"
      spec:
        backoffLimit: 3
        ttlSecondsAfterFinished: 300
        template:
          spec:
            restartPolicy: OnFailure
            containers:
              - name: mc
                image: quay.io/minio/mc:latest
                command:
                  - /bin/sh
                  - -c
                  - |
                    mc alias set minio http://troshka-minio:9000 {{ troshka_minio_root_user }} {{ troshka_minio_root_password }}
                    mc mb --ignore-existing minio/{{ troshka_s3_bucket }}
```

- [ ] **Step 7: Create `tasks/backend.yaml`**

```yaml
---
- name: Create backend ConfigMap
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: ConfigMap
      metadata:
        name: troshka-config
        namespace: "{{ troshka_namespace }}"
      data:
        config.yaml: |
          app:
            port: 8200
            host: "0.0.0.0"
            external_url: "https://{{ _troshka_route_host }}"
          auth:
            oauth_enabled: {{ troshka_oauth_enabled | lower }}
            admin_users: "{{ troshka_admin_users }}"
            operator_users: "{{ troshka_operator_users }}"
          {% if troshka_deploy_minio | bool %}
          s3:
            endpoint_url: "http://troshka-minio.{{ troshka_namespace }}.svc:9000"
            bucket: "{{ troshka_s3_bucket }}"
          {% endif %}
          defaults:
            run_timer_hours: {{ troshka_run_timer_hours }}
            lifetime_days: {{ troshka_lifetime_days }}
            max_vms_per_project: 20
            max_projects_per_user: 10
            user_library_quota_gb: 500
          overcommit:
            cpu_ratio: 4.0
            ram_ratio: 1.5

- name: Deploy backend
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: troshka-backend
        namespace: "{{ troshka_namespace }}"
        labels:
          app.kubernetes.io/name: troshka-backend
      spec:
        replicas: 1
        selector:
          matchLabels:
            app.kubernetes.io/name: troshka-backend
        template:
          metadata:
            labels:
              app.kubernetes.io/name: troshka-backend
          spec:
            containers:
              - name: backend
                image: "{{ troshka_backend_image }}"
                ports:
                  - containerPort: 8200
                    name: http
                env:
                  - name: TROSHKA_DATABASE__URL
                    valueFrom:
                      secretKeyRef:
                        name: troshka-secrets
                        key: database-url
                  - name: TROSHKA_AUTH__JWT_SECRET
                    valueFrom:
                      secretKeyRef:
                        name: troshka-secrets
                        key: jwt-secret
                  - name: TROSHKA_AUTH__ENCRYPTION_KEY
                    valueFrom:
                      secretKeyRef:
                        name: troshka-secrets
                        key: encryption-key
                  - name: TROSHKA_AWS__ACCESS_KEY_ID
                    valueFrom:
                      secretKeyRef:
                        name: troshka-secrets
                        key: s3-access-key
                        optional: true
                  - name: TROSHKA_AWS__SECRET_ACCESS_KEY
                    valueFrom:
                      secretKeyRef:
                        name: troshka-secrets
                        key: s3-secret-key
                        optional: true
                volumeMounts:
                  - name: config
                    mountPath: /opt/app-root/src/config/config.yaml
                    subPath: config.yaml
                readinessProbe:
                  httpGet:
                    path: /api/v1/health
                    port: 8200
                  initialDelaySeconds: 5
                  periodSeconds: 10
                livenessProbe:
                  httpGet:
                    path: /api/v1/health
                    port: 8200
                  initialDelaySeconds: 15
                  periodSeconds: 30
                resources:
                  requests:
                    cpu: "{{ troshka_backend_cpu_request }}"
                    memory: 512Mi
                  limits:
                    memory: "{{ troshka_backend_memory_limit }}"
            volumes:
              - name: config
                configMap:
                  name: troshka-config

- name: Create backend Service
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: Service
      metadata:
        name: troshka-backend
        namespace: "{{ troshka_namespace }}"
      spec:
        selector:
          app.kubernetes.io/name: troshka-backend
        ports:
          - port: 8200
            targetPort: 8200
            name: http

- name: Wait for backend to be ready
  kubernetes.core.k8s_info:
    api_version: apps/v1
    kind: Deployment
    name: troshka-backend
    namespace: "{{ troshka_namespace }}"
  register: _backend_deploy
  until: (_backend_deploy.resources[0].status.readyReplicas | default(0)) >= 1
  retries: 30
  delay: 10
```

- [ ] **Step 8: Create `tasks/migrate.yaml`**

```yaml
---
- name: Run Alembic migration
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: batch/v1
      kind: Job
      metadata:
        name: "troshka-migrate-{{ lookup('pipe', 'date +%s') }}"
        namespace: "{{ troshka_namespace }}"
      spec:
        backoffLimit: 3
        ttlSecondsAfterFinished: 600
        template:
          spec:
            restartPolicy: OnFailure
            containers:
              - name: migrate
                image: "{{ troshka_backend_image }}"
                command: ["python3", "-m", "alembic", "upgrade", "head"]
                env:
                  - name: TROSHKA_DATABASE__URL
                    valueFrom:
                      secretKeyRef:
                        name: troshka-secrets
                        key: database-url
                volumeMounts:
                  - name: config
                    mountPath: /opt/app-root/src/config/config.yaml
                    subPath: config.yaml
            volumes:
              - name: config
                configMap:
                  name: troshka-config
  register: _migration_job

- name: Wait for migration to complete
  kubernetes.core.k8s_info:
    api_version: batch/v1
    kind: Job
    name: "{{ _migration_job.result.metadata.name }}"
    namespace: "{{ troshka_namespace }}"
  register: _migration_status
  until: >-
    (_migration_status.resources[0].status.succeeded | default(0)) >= 1
    or (_migration_status.resources[0].status.failed | default(0)) >= 1
  retries: 24
  delay: 5

- name: Fail if migration failed
  ansible.builtin.fail:
    msg: "Database migration failed. Check job logs: oc logs job/{{ _migration_job.result.metadata.name }} -n {{ troshka_namespace }}"
  when: (_migration_status.resources[0].status.failed | default(0)) >= 1
```

- [ ] **Step 9: Create `tasks/frontend.yaml`**

```yaml
---
- name: Determine route host
  ansible.builtin.set_fact:
    _troshka_route_host: "{{ troshka_route_host | default('troshka-' ~ troshka_namespace ~ '.apps.' ~ _cluster_domain, true) }}"
  when: _troshka_route_host is not defined

- name: Get cluster domain
  kubernetes.core.k8s_info:
    api_version: config.openshift.io/v1
    kind: Ingress
    name: cluster
  register: _ingress_config
  when: troshka_route_host | default('', true) | length == 0

- name: Set route host from cluster domain
  ansible.builtin.set_fact:
    _troshka_route_host: "troshka-{{ troshka_namespace }}.{{ _ingress_config.resources[0].spec.domain }}"
  when:
    - troshka_route_host | default('', true) | length == 0
    - _troshka_route_host is not defined

- name: Use provided route host
  ansible.builtin.set_fact:
    _troshka_route_host: "{{ troshka_route_host }}"
  when: troshka_route_host | default('', true) | length > 0

- name: Deploy frontend
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: troshka-frontend
        namespace: "{{ troshka_namespace }}"
        labels:
          app.kubernetes.io/name: troshka-frontend
      spec:
        replicas: 1
        selector:
          matchLabels:
            app.kubernetes.io/name: troshka-frontend
        template:
          metadata:
            labels:
              app.kubernetes.io/name: troshka-frontend
          spec:
            containers:
              - name: frontend
                image: "{{ troshka_frontend_image }}"
                ports:
                  - containerPort: 3000
                    name: http
                env:
                  - name: BACKEND_URL
                    value: "http://troshka-backend.{{ troshka_namespace }}.svc:8200"
                readinessProbe:
                  httpGet:
                    path: /
                    port: 3000
                  initialDelaySeconds: 5
                  periodSeconds: 10
                resources:
                  requests:
                    cpu: "{{ troshka_frontend_cpu_request }}"
                    memory: 256Mi
                  limits:
                    memory: "{{ troshka_frontend_memory_limit }}"

- name: Create frontend Service
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: Service
      metadata:
        name: troshka-frontend
        namespace: "{{ troshka_namespace }}"
      spec:
        selector:
          app.kubernetes.io/name: troshka-frontend
        ports:
          - port: 3000
            targetPort: 3000
            name: http

- name: Create Route
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: route.openshift.io/v1
      kind: Route
      metadata:
        name: troshka
        namespace: "{{ troshka_namespace }}"
      spec:
        host: "{{ _troshka_route_host }}"
        tls:
          termination: "{{ 'reencrypt' if (troshka_oauth_enabled | bool) else 'edge' }}"
          insecureEdgeTerminationPolicy: Redirect
        to:
          kind: Service
          name: "{{ 'troshka-oauth-proxy' if (troshka_oauth_enabled | bool) else 'troshka-frontend' }}"
          weight: 100
        port:
          targetPort: "{{ 'proxy' if (troshka_oauth_enabled | bool) else 'http' }}"

- name: Display route
  ansible.builtin.debug:
    msg: "Route: https://{{ _troshka_route_host }}"
```

- [ ] **Step 10: Create `tasks/oauth.yaml`**

```yaml
---
- name: Check if OAuth secret exists
  kubernetes.core.k8s_info:
    api_version: v1
    kind: Secret
    name: troshka-oauth-proxy
    namespace: "{{ troshka_namespace }}"
  register: _existing_oauth_secret

- name: Generate OAuth secrets if not present
  when: _existing_oauth_secret.resources | length == 0
  block:
    - name: Generate OAuth client secret
      ansible.builtin.set_fact:
        _oauth_client_secret: "{{ lookup('password', '/dev/null chars=ascii_letters,digits length=32') }}"

    - name: Generate OAuth cookie secret
      ansible.builtin.command: python3 -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
      register: _cookie_secret_result
      changed_when: false

    - name: Set cookie secret fact
      ansible.builtin.set_fact:
        _oauth_cookie_secret: "{{ _cookie_secret_result.stdout }}"

    - name: Create OAuth proxy secret
      kubernetes.core.k8s:
        state: present
        definition:
          apiVersion: v1
          kind: Secret
          metadata:
            name: troshka-oauth-proxy
            namespace: "{{ troshka_namespace }}"
          type: Opaque
          stringData:
            client-secret: "{{ _oauth_client_secret }}"
            cookie-secret: "{{ _oauth_cookie_secret }}"

- name: Load existing OAuth secrets
  when: _existing_oauth_secret.resources | length > 0
  ansible.builtin.set_fact:
    _oauth_client_secret: "{{ _existing_oauth_secret.resources[0].data['client-secret'] | b64decode }}"

- name: Create OAuth ServiceAccount
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: ServiceAccount
      metadata:
        name: troshka-oauth
        namespace: "{{ troshka_namespace }}"
        annotations:
          serviceaccounts.openshift.io/oauth-redirectreference.primary: >-
            {"kind":"OAuthRedirectReference","apiVersion":"v1","reference":{"kind":"Route","name":"troshka"}}

- name: Create OAuthClient
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: oauth.openshift.io/v1
      kind: OAuthClient
      metadata:
        name: "troshka-{{ troshka_namespace }}"
      grantMethod: auto
      secret: "{{ _oauth_client_secret }}"
      redirectURIs:
        - "https://{{ _troshka_route_host }}/oauth/callback"

- name: Deploy OAuth proxy
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: troshka-oauth-proxy
        namespace: "{{ troshka_namespace }}"
        labels:
          app.kubernetes.io/name: troshka-oauth-proxy
      spec:
        replicas: 1
        selector:
          matchLabels:
            app.kubernetes.io/name: troshka-oauth-proxy
        template:
          metadata:
            labels:
              app.kubernetes.io/name: troshka-oauth-proxy
          spec:
            serviceAccountName: troshka-oauth
            containers:
              - name: oauth-proxy
                image: registry.redhat.io/openshift4/ose-oauth-proxy-rhel9:latest
                ports:
                  - containerPort: 8443
                    name: proxy
                args:
                  - --provider=openshift
                  - --https-address=:8443
                  - --http-address=
                  - --upstream=http://troshka-frontend.{{ troshka_namespace }}.svc:3000
                  - --tls-cert=/etc/tls/private/tls.crt
                  - --tls-key=/etc/tls/private/tls.key
                  - --cookie-secret-file=/etc/oauth/cookie-secret
                  - --client-id=troshka-{{ troshka_namespace }}
                  - --client-secret-file=/etc/oauth/client-secret
                  - --openshift-service-account=troshka-oauth
                  - --openshift-ca=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
                  - --pass-user-headers=true
                  - --pass-access-token=false
                  - --skip-auth-regex=^/api/v1/health
                volumeMounts:
                  - name: tls
                    mountPath: /etc/tls/private
                  - name: oauth-secrets
                    mountPath: /etc/oauth
                resources:
                  requests:
                    cpu: 50m
                    memory: 64Mi
                  limits:
                    memory: 128Mi
            volumes:
              - name: tls
                secret:
                  secretName: troshka-oauth-tls
              - name: oauth-secrets
                secret:
                  secretName: troshka-oauth-proxy

- name: Create OAuth proxy Service
  kubernetes.core.k8s:
    state: present
    definition:
      apiVersion: v1
      kind: Service
      metadata:
        name: troshka-oauth-proxy
        namespace: "{{ troshka_namespace }}"
        annotations:
          service.beta.openshift.io/serving-cert-secret-name: troshka-oauth-tls
      spec:
        selector:
          app.kubernetes.io/name: troshka-oauth-proxy
        ports:
          - port: 8443
            targetPort: 8443
            name: proxy
```

- [ ] **Step 11: Create `undeploy.yaml`**

```yaml
---
- name: Undeploy Troshka from OpenShift
  hosts: localhost
  connection: local
  gather_facts: false

  tasks:
    - name: Delete OAuthClient
      kubernetes.core.k8s:
        state: absent
        api_version: oauth.openshift.io/v1
        kind: OAuthClient
        name: "troshka-{{ troshka_namespace }}"
      ignore_errors: true

    - name: Delete namespace (removes everything)
      kubernetes.core.k8s:
        state: absent
        definition:
          apiVersion: v1
          kind: Namespace
          metadata:
            name: "{{ troshka_namespace }}"

    - name: Confirm deletion
      ansible.builtin.debug:
        msg: "Troshka undeployed. Namespace '{{ troshka_namespace }}' deleted."
```

- [ ] **Step 12: Validate playbook syntax**

```bash
ansible-playbook deploy/ansible/deploy.yaml --syntax-check
ansible-playbook deploy/ansible/undeploy.yaml --syntax-check
```

Expected: both pass syntax check.

- [ ] **Step 13: Commit**

```bash
git add deploy/ansible/
git commit -m "feat: add Ansible deploy/undeploy playbooks for OCP installation"
```

---

### Task 5: GitHub Actions CI Workflow

Build and push container images on push to main and on tags.

**Files:**
- Create: `.github/workflows/build-images.yml`

**Interfaces:**
- Consumes: Containerfiles from Task 1
- Produces: images pushed to `quay.io/rhpds/troshka-{backend,frontend}`

- [ ] **Step 1: Create workflow file**

Create `.github/workflows/build-images.yml`:

```yaml
name: Build and Push Container Images

on:
  push:
    branches: [main]
    tags: ['v*']
    paths:
      - 'src/backend/**'
      - 'src/frontend/**'
      - 'src/troshkad/**'
      - 'deploy/containerfiles/**'

jobs:
  build-backend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set image tags
        id: tags
        run: |
          SHA="${{ github.sha }}"
          SHORT_SHA="${SHA:0:8}"
          echo "sha_tag=${SHORT_SHA}" >> "$GITHUB_OUTPUT"
          if [[ "${{ github.ref }}" == refs/tags/v* ]]; then
            echo "version_tag=${{ github.ref_name }}" >> "$GITHUB_OUTPUT"
          fi

      - name: Build backend image
        run: |
          podman build \
            -f deploy/containerfiles/Containerfile.backend \
            -t quay.io/rhpds/troshka-backend:${{ steps.tags.outputs.sha_tag }} \
            -t quay.io/rhpds/troshka-backend:latest \
            .

      - name: Tag version (if tag push)
        if: startsWith(github.ref, 'refs/tags/v')
        run: |
          podman tag \
            quay.io/rhpds/troshka-backend:${{ steps.tags.outputs.sha_tag }} \
            quay.io/rhpds/troshka-backend:${{ steps.tags.outputs.version_tag }}

      - name: Push to Quay.io
        run: |
          podman login -u="${{ secrets.QUAY_USERNAME }}" -p="${{ secrets.QUAY_PASSWORD }}" quay.io
          podman push quay.io/rhpds/troshka-backend:${{ steps.tags.outputs.sha_tag }}
          podman push quay.io/rhpds/troshka-backend:latest
          if [[ -n "${{ steps.tags.outputs.version_tag }}" ]]; then
            podman push quay.io/rhpds/troshka-backend:${{ steps.tags.outputs.version_tag }}
          fi

  build-frontend:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set image tags
        id: tags
        run: |
          SHA="${{ github.sha }}"
          SHORT_SHA="${SHA:0:8}"
          echo "sha_tag=${SHORT_SHA}" >> "$GITHUB_OUTPUT"
          if [[ "${{ github.ref }}" == refs/tags/v* ]]; then
            echo "version_tag=${{ github.ref_name }}" >> "$GITHUB_OUTPUT"
          fi

      - name: Build frontend image
        run: |
          podman build \
            -f deploy/containerfiles/Containerfile.frontend \
            -t quay.io/rhpds/troshka-frontend:${{ steps.tags.outputs.sha_tag }} \
            -t quay.io/rhpds/troshka-frontend:latest \
            .

      - name: Tag version (if tag push)
        if: startsWith(github.ref, 'refs/tags/v')
        run: |
          podman tag \
            quay.io/rhpds/troshka-frontend:${{ steps.tags.outputs.sha_tag }} \
            quay.io/rhpds/troshka-frontend:${{ steps.tags.outputs.version_tag }}

      - name: Push to Quay.io
        run: |
          podman login -u="${{ secrets.QUAY_USERNAME }}" -p="${{ secrets.QUAY_PASSWORD }}" quay.io
          podman push quay.io/rhpds/troshka-frontend:${{ steps.tags.outputs.sha_tag }}
          podman push quay.io/rhpds/troshka-frontend:latest
          if [[ -n "${{ steps.tags.outputs.version_tag }}" ]]; then
            podman push quay.io/rhpds/troshka-frontend:${{ steps.tags.outputs.version_tag }}
          fi
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/build-images.yml
git commit -m "ci: add GitHub Actions workflow to build and push container images"
```

---

### Task 6: Integration Test — Local Build and Deploy

Test the full pipeline: build images locally, deploy to an OCP cluster with the Ansible playbook.

**Files:**
- No new files — this is a validation task

**Interfaces:**
- Consumes: everything from Tasks 1-5

- [ ] **Step 1: Build images locally**

```bash
podman build -f deploy/containerfiles/Containerfile.backend -t troshka-backend:test .
podman build -f deploy/containerfiles/Containerfile.frontend -t troshka-frontend:test .
```

Expected: both build successfully.

- [ ] **Step 2: Push to a test registry (or use local)**

If testing on a cluster with access to a registry:

```bash
podman tag troshka-backend:test quay.io/rhpds/troshka-backend:dev
podman tag troshka-frontend:test quay.io/rhpds/troshka-frontend:dev
podman push quay.io/rhpds/troshka-backend:dev
podman push quay.io/rhpds/troshka-frontend:dev
```

- [ ] **Step 3: Deploy with Ansible (all-in-one, no SSO)**

```bash
ansible-playbook deploy/ansible/deploy.yaml \
  -e troshka_deploy_postgres=true \
  -e troshka_deploy_minio=true \
  -e troshka_backend_image=quay.io/rhpds/troshka-backend:dev \
  -e troshka_frontend_image=quay.io/rhpds/troshka-frontend:dev \
  -e troshka_namespace=troshka-test
```

Expected: all tasks succeed. PostgreSQL, MinIO, backend, frontend all running.

- [ ] **Step 4: Verify the deployment**

```bash
oc get pods -n troshka-test
oc get route -n troshka-test
```

Expected: 4 pods running (postgres, minio, backend, frontend), 1 route.

Open the route URL in a browser — should see the Troshka UI in dev-mode (auto-authenticated as admin).

- [ ] **Step 5: Verify API health**

```bash
curl -k https://$(oc get route troshka -n troshka-test -o jsonpath='{.spec.host}')/api/v1/health
```

Expected: `{"status":"ok"}`

- [ ] **Step 6: Clean up test deployment**

```bash
ansible-playbook deploy/ansible/undeploy.yaml -e troshka_namespace=troshka-test
```

Expected: namespace deleted, all resources cleaned up.

- [ ] **Step 7: Commit any fixes discovered during testing**

If any adjustments were needed during testing, commit them:

```bash
git add -A
git commit -m "fix: adjustments from integration testing OCP deployment"
```
