# Deployment (Helm / Kustomize / Ansible) Reference

> Extracted from the top-level `CLAUDE.md` to keep it lean. Read this file when working on the topics below.

## Deployment

Three deployment methods — all produce equivalent results:
### Helm Chart (`deploy/helm/`)
```bash
helm install troshka deploy/helm/ -n troshka \
  --set postgres.deploy=true \
  --set auth.oauthEnabled=true \
  --set auth.allowedGroups="rhpds-admins\,troshka-users" \
  --set auth.adminGroups="rhpds-admins" \
  --set route.host=troshka.apps.cluster.example.com
```
- Full-stack chart: backend, frontend, PostgreSQL, S4, OAuth proxy, RBAC, migration Job
- All components conditional via `values.yaml` toggles (`postgres.deploy`, `s4.deploy`, `auth.oauthEnabled`)
- Global `deploy: false` suppresses all resources (ArgoCD pattern)
- Migration runs as Helm pre-install/pre-upgrade hook
- Secrets auto-generated on first install, preserved on upgrade (`helm.sh/resource-policy: keep`)
- ConfigMap changes trigger pod restart via `checksum/config` annotation
- **OAuth proxy has NO auto-rollout** — changing `--skip-auth-regex` or other args requires a manual `oc rollout restart deployment/troshka-oauth-proxy`. Do NOT add a checksum annotation to the OAuth proxy pod template — the template contains `randAlphaNum` for secrets, so any re-render regenerates the client-secret and breaks OAuth login.
### Kustomize (`deploy/base/` + `deploy/overlays/`)
```bash
oc apply -k deploy/overlays/postgres   # base + in-cluster PostgreSQL
oc apply -k deploy/overlays/sso        # base + OAuth proxy
oc apply -k deploy/overlays/s4         # base + in-cluster S4
```
- Base: namespace, backend (Deployment + Service + ConfigMap + Secret), frontend (Deployment + Service + Route), RBAC (ServiceAccount + ClusterRole + ClusterRoleBinding)
- Overlays compose additively — apply multiple overlays by creating a new overlay that references them
### Ansible (`deploy/ansible/`)
```bash
ansible-playbook deploy/ansible/deploy.yaml \
  -e kubeconfig=~/secrets/cluster.kubeconfig \
  -e troshka_deploy_postgres=true \
  -e troshka_oauth_enabled=true \
  -e troshka_allowed_groups="rhpds-admins,troshka-users"
```
- Variables in `deploy/ansible/inventory/group_vars/all.yaml`
- Task order: namespace → RBAC → secrets → PostgreSQL → S4 → backend → migration → frontend → OAuth
- Secrets auto-generated on first deploy, preserved on re-deploy
- Undeploy: `ansible-playbook deploy/ansible/undeploy.yaml`
### OCP Virt package repo
Standalone playbook (not part of `deploy.yaml`) for dedicated-CI ocpvirt hosts:

```bash
# route_host and iso_library_item_name: see ocpvirt.pkg_repo in src/backend/config/config.yaml
ansible-playbook deploy/ansible/pkg-repo.yaml \
  -e pkg_repo_password='<from-vault>' \
  -e pkg_repo_route_host='<from config.yaml>' \
  -e pkg_repo_iso_library_item_name='<from config.yaml>'
```

Extracts RHEL `BaseOS`/`AppStream` from the central-S4 DVD ISO onto a PVC and serves them via nginx + basic auth. Backend host provisioning reads `ocpvirt.pkg_repo` from config (provider overrides optional). Full design: [`ocpvirt-package-repo.md`](ocpvirt-package-repo.md).
### Container Images
- Built by GitHub Actions on push to `main` or version tags
- Images at `quay.io/redhat-gpte/troshka-{backend,frontend,operator,dnsmasq,gateway,tools,bmc,vnc-proxy,ops-pod}`
- Containerfiles in `deploy/containerfiles/` (backend, frontend) and `src/operator/images/` (operator components)
- `ops-pod` is the multi-cluster OCP bastionless install execution-environment (EE): `oc`/`openshift-install` + `ansible-core`/`ansible-navigator` + the `troshka.cloud` collection + Redfish/`curl` tooling. Built in the `build-operator` job (`--build-arg OCP_VERSION=<x.y.z>` to pin). Backend resolves the ref from config `ocp.ops_pod_image`. See `src/operator/images/ops-pod/README.md`.

## App Update Notification

Admin-visible banner announcing that a newer Troshka backend/frontend is available, plus a one-click apply. Service: `src/backend/app/services/app_updater.py`; router: `src/backend/app/api/updates.py` (prefix `/update`, registered in `main.py`). Mirrors `operator_updater` but targets the app's *own* images in-cluster (pod ServiceAccount) rather than a remote provider cluster.
### Modes
Resolved once at startup by `resolve_mode()`. Config `mode: auto` (default) auto-detects; an explicit `dev`/`image`/`disabled` overrides detection.
- **`dev`** — auto-selected when `auth.oauth_enabled` is false (the canonical dev-mode signal). No polling; status is computed on demand by comparing the newest `*.py` mtime under `src/backend` against the backend process start time (`up_to_date` when nothing changed since start).
- **`image`** — deployed with OAuth on and no ArgoCD label. A daemon thread polls every `poll_interval` seconds, comparing the running pod image digest (label selector `app=troshka-backend` / `app=troshka-frontend`, own namespace) against the registry digest on quay.io for both components. The **comparison tag is auto-detected per component from each running Deployment's image** (`_read_deployment_tag`): the tag after the last `:` in `spec.template.spec.containers[0].image` — e.g. `latest` for dedicated-CI deploys, `production` for production-pinned deploys. Parsing is registry-port- and digest-safe (only the final path segment can carry a tag; `@sha256:` pins and un-tagged refs fall back to `app_update.tag`). `up_to_date` is false only when a component's current and available digests both resolve and differ — digest fetch failures keep the last snapshot, never a false "update available". Also tracks `rolling_out` from Deployment replica readiness.
- **`disabled`** — auto-selected when the backend's own Deployment carries the `argocd.argoproj.io/instance` label (Argo owns updates). Feature is fully off; status returns `{"mode": "disabled"}` and the UI renders nothing.
### Config (`app_update` block in `config.yaml`)
```yaml
app_update:
  mode: auto          # auto | dev | image | disabled
  registry: quay.io
  repo: redhat-gpte
  tag: production      # fallback comparison tag; the tag is normally auto-detected per component from the running Deployment's image (e.g. latest for dedicated CI), and this value is used only when detection fails or the image has no tag
  poll_interval: 300
```
- Env overrides via Dynaconf: `TROSHKA_APP_UPDATE__MODE`, `TROSHKA_APP_UPDATE__TAG`, etc.
- Key is `app_update` (not `update`) to avoid colliding with Dynaconf's `.update()` method.
### Endpoints (both `AdminUser` — 403 for non-admins)
- `GET /api/v1/update/status` — feeds the banner: `{mode, up_to_date, rolling_out, components{backend,frontend:{current,available}}}`.
- `POST /api/v1/update/apply` — mode-specific; returns `400` in `disabled` mode.
### Apply behavior
- **image** — in-cluster rollout-restart of both `troshka-backend` and `troshka-frontend` Deployments (patch `kubectl.kubernetes.io/restartedAt` annotation); the repull of the running tag (auto-detected, e.g. `latest` or `production`) picks up the new digest. Returns `{"status": "rolling_out"}`. Requires the `troshka-self-update` Role/RoleBinding bound to the backend ServiceAccount, scoping `get`/`patch` to the named `troshka-backend`/`troshka-frontend` deployments plus `get`/`list` on `pods`, and the `POD_NAMESPACE` downward-API env var on the backend Deployment for own-namespace discovery (falls back to the SA namespace file, then `troshka`). Missing RBAC surfaces the k8s `403` to the admin rather than a false success.
- **dev** — spawns a detached `./dev-services.sh restart backend` (`Popen(start_new_session=True)` from repo root); returns `{"status": "restarting"}` before uvicorn is killed. Frontend hot-reloads independently.
### Frontend banner (`src/frontend/src/app/layout.tsx`)
- Admin-only (`user?.role === "admin"`), polls `/update/status` every 60s. When `up_to_date` is false, renders a dismissible info banner in the existing `backendDown` slot with an "Apply update" (image) / "Restart backend" (dev) button; shows "Update in progress" while `rolling_out`. Dismissal persists in `localStorage` keyed by target digest / process-change marker, so a newer update re-shows it.
