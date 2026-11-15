# Deployment (Helm / Kustomize / Ansible) Reference

For Argo setup or adoption of an existing dev Helm release, see
[Argo bootstrap](argocd-bootstrap.md). Dev follows `main` with `latest` application
images; adoption preserves active overlays until the first rollout is reviewed.

> Extracted from the top-level `CLAUDE.md` to keep it lean. Read this file when working on the topics below.

## Deployment

### Full production release

From a clean, committed checkout, run `./scripts/deploy-full.sh`. It creates a
UTC release tag (`vYYYY.MM.DD-HHMMSS`), atomically pushes HEAD to `main` and the
tag, waits for the tag's full image build, upgrades host prerequisites, verifies
collector readiness, and promotes that release to the existing `production`
image tags. `main` remains the integration branch;
Argo CD Image Updater still deploys `production` digests.

Before promotion, the script requires dev Argo to run the exact release digests
through `latest`, checks every active app worker deployment, and requires fresh
dev readiness and shared/project accounting through the normal queues. A failed
dev check exits before changing production image tags. Dev collector setup must
already be ready for this gate, including when a setup plan is supplied for other
hosts. Confirmed queue collisions may wait for fresh evidence; stale reports or
an unavailable/ambiguous enqueue never satisfy the gate.

Use `--tag vNAME` to choose a release name. To resume a failed or interrupted
deployment, check out that release and run
`./scripts/deploy-full.sh --skip-push --tag vNAME`. If GitHub has not registered
the build when queried, the script reports this resume command without promoting
anything. Never move a published release tag.

After the dev checks, deployment applies host CRDs/RBAC and rolls the operators
using the named release image tag. Project helper pods are left until after app
promotion. An operator failure stops the release before production tags change;
operators already updated remain on the named release and can be verified on
resume. This stage does not request VM stops or changes to Ceph.

After host prerequisites, full deployment runs `collector_fleet_preflight.py`
against both the production and permanent dev applications. It reads configured
native hosts from each application's database, including hosts with no projects,
and requires exactly one protected profile with an existing enrollment anchor for
each provider. An inaccessible worker, malformed profile, missing enrollment, or
missing host profile blocks the release and produces a retained JSON report.
The probe runs read-only in the collection worker; no credentials are returned.
This is a configuration gate, not a fresh accounting or capacity acceptance test.
With `--collector-setup-plan /absolute/path/to/approved.json`, a read-only setup
validation and application run before the fleet gate. The plan must cover both applications and
every configured native host. Existing profiles cannot be overwritten. Deployment
installs immutable protected configuration, rotates
seven-day collector-only tokens, waits for both collector worker deployments,
and enrolls explicitly authorized empty domains. Existing enrollment anchors are
validated and preserved. Fresh fleet accounting must then pass before production
app promotion. Neither scheduling nor quota enablement is changed. If profiles or
accepted storage-version evidence are still missing, host upgrades remain applied
and app promotion stops; deployment does not manufacture acceptance evidence.

The operator creates project namespace permissions on first deployment; it does
not create the cluster's collector profile or initial enrollment. Cluster setup
is deliberately separate so an empty host can be ready before its first project.

Every full release ends with `collector_release_check.py`. It submits readiness,
shared-storage, and project collection through the normal RQ queues, then checks
fresh persisted observations and each project's latest accepted report. Old
successes, newer failures, changed enrollment, active cleanup, and incomplete
attempts block success. This is accounting verification, not new storage-version
acceptance or permission to enable quotas.

Setup uses a private retained directory, printed before preparation. It contains
credentials after apply; do not publish it. An interrupted apply is never retried
automatically. Inspect its journal, installed immutable objects, application
configuration, and enrollment before preparing a new attempt. Old configuration
and transport objects are retained for recovery; token expiry still applies.

The setup plan has an `applications` array. Each entry specifies `kubeconfig`,
`namespace`, Argo `application`, `transports` (protected kubeconfig filename to
administrator kubeconfig path), and optional `additions`. Each addition contains
the normal collector `profile` and `files` mapping `pins`, `inventory`, and
`backend_config` to reviewed local JSON files. Pins must already carry accepted
initialization evidence and explicit empty-domain authorization. Setup never
invents those approvals. The transport map must cover existing and added profiles.
With no additions, the same plan rotates credentials while preserving profiles.
All referenced paths should be absolute. The applications must already have their
collector worker configuration in Helm `valuesObject`; conflicting Helm parameter
overrides are rejected. Unsupported or unapproved clusters still block release.

Run the check independently without pushing or deploying:

```bash
python3 scripts/collector_fleet_preflight.py \
  --application "$HOME/secrets/ocpv-infra01.dal12.infra.demo.redhat.com.kubeconfig" troshka \
  --application "$HOME/secrets/ocpvdev01.dal13.infra.demo.redhat.com.kubeconfig" troshka-dev \
  --output /tmp/troshka-collector-preflight.json
```

Full releases apply `infra/storage-collector-base-rbac.yaml` to every operator
cluster, including hosts with no projects. It installs the dedicated collector
account, read-only PV/snapshot inventory, and named CSI installation reads.
It does not install credentials, accounting profiles, project namespace grants,
or toolbox exec access. `--skip-operators` also skips this baseline installation.
The baseline allows GET of namespace metadata so newly created project namespaces
can be verified without editing a cluster-wide allowlist after every deployment.
`infra/storage-collector-project-rbac.yaml` defines the read-only project
ClusterRole without a ClusterRoleBinding. The operator creates its RoleBinding
only in each new deployment's project namespace and verifies any existing binding
before reuse. Cleanup recognizes that exact role and collector ServiceAccount.
Neither collector role grants Secret access. The role is never bound across
the cluster or in shared cache namespaces.

For the remaining scoped native storage collection access, add
`--collector-access-plan /absolute/path/to/approved-plan.json`. This explicitly
opts in only the provider clusters and namespaces in that administrator-reviewed
plan; it does not grant collector access on all seven operator clusters.
The plan uses the online-enrollment preflight `domains` entries: `backend.kubeconfig`,
the pinned `backend.toolbox` namespace/name/UID, and `pins.provider_clusters`,
`pins.project_roots`, and `pins.cache_roots`. Keep credentials out of this file.
Use an absolute kubeconfig path for each target. No live UIDs or credentials are
checked into the permission template.

`scripts/deploy_collector_rbac.py` renders the read-only namespace permissions
from `infra/storage-collector-rbac.yaml` (JSON syntax, valid YAML), adds inventory
access and an exact-toolbox-pod exec grant, and server-dry-runs every target.
It rechecks cluster, namespace, project, and toolbox identities before applying
the grants, ahead of image promotion. A changed toolbox UID requires a reviewed
plan update. Toolbox exec carries Ceph privileges; the API cannot restrict the
commands executed inside that pod. Project Role/RoleBinding objects are owned by
their pinned TroshkaProject; cache grants are shared and unowned. This does not
issue credentials, configure collector profiles, or enable quota enforcement.
Full project custody checks, including Secret identities and key sets, run with
the existing worker/operator credential. Collectors check the remaining workload
and storage identities without reading Secrets. They cannot approve namespace
deletion, and settlement still requires namespace absence. Shared-cache grants
also omit ConfigMaps because cache discovery does not require those checks.
CSI readiness also needs installation metadata. Set each domain's optional
`readiness_access` object with `namespace`, `storage_class`, `driver`,
`controller`, `node`, `service_accounts` (list), and `priority_classes` (list).
These are exact reviewed names. The renderer adds named GET grants plus pod and
ReplicaSet LIST grants only in that namespace; it adds no Secret reads or exec
access. Collection uses its protected TLS-verified kubeconfig for these reads,
independently of the ordinary provider credentials.
If CRI reports a configuration digest on one node and a manifest digest on
others, the protected profile can record reviewed `image_aliases` under its
`target.csi_installation.controller` or `.node` identity. Each entry specifies
`container`, `reported_digest`, `manifest_digest`, and `validation_reference`.
Both digests use `sha256:` notation; the manifest must match that container's
pinned `images` entry. Retain evidence from the node's `crictl inspecti` output
linking its `status.id` to the exact manifest in `status.repoDigests`, and use a
content-hash reference to that reviewed evidence. Never infer equivalence from
the configured image name or tag. Aliases are administrator-provided protected
configuration, not values accepted from pod metadata. Unreviewed bare digests,
unknown images, and all existing readiness/ownership checks still fail closed.
Empty alias lists are omitted from serialization so existing profile hashes
remain unchanged. Profile preparation may pass the reviewed installation to
`target_from_reads`; all installation fields are still independently observed.
For single-provider enrollment verification, `import_kubevirt_storage.py verify
--kubeconfig /absolute/path/to/collector.kubeconfig` explicitly selects the same
protected transport. It requires verified TLS and still checks the pinned cluster
identity. Without that option, verification uses the configured provider credential;
online compatibility verification checks both cluster identities and uses the
provider credential for full custody reads while retaining the collector transport
for storage reads.
New deployments receive their project binding automatically. Existing projects
without that binding, cache roots, or replacement toolbox identities need a freshly
reviewed access plan; the deploy script never expands scope through discovery.
Omitting the option leaves existing
collector grants untouched. For an online compatibility release also use
`--skip-project-pods` to avoid restarting project helper services.

The script preflights all ten images and cluster access, requires idle RQ queues,
promotes and verifies every destination digest, applies CRDs and RBAC without
overwriting SCC membership, and rolls operators and persistent project services
across the seven configured clusters. It checks every running replica, including
all workers and tunnels, then compares the runtime template schema with the
release checkout and checks backend HTTP health. Transient ops-pod and terminal
images are promoted for subsequent jobs; active jobs are not forcibly restarted.

A failed cluster or unhealthy deployment makes the command fail, even if that
failure predates the release. Inspect its errors before resuming. The existing
`--skip-operators`, `--skip-project-pods`, and `--sync-ns-rbac` options remain
available for deliberately narrower runs. Standalone promotion now requires an
explicit source: `./scripts/promote-to-production.sh vNAME`; it never defaults
to `latest`.

### Installation methods

For a fresh SSO deployment, supply the first admin's SSO identity using Helm
`--set auth.oauthEnabled=true --set-string auth.initialAdmin=person@example.com`
or Ansible `-e troshka_oauth_enabled=true -e troshka_initial_admin=person@example.com`.
The migration job seeds that enabled admin account in the database. Once access
has been initialized, this option cannot restore a deleted admin or override role
changes; manage all later access through **Admin → Users**. Without an initial
admin or a legacy policy to import, SSO logins are denied until an admin is seeded.
Dedicated CI supplies the requester identity to the same bootstrap command during
its one-time provision. Infrastructure configuration remains in `troshka-config`.

Existing deployments import their old allowlists/group-derived access once, then
ignore those settings for authorization. The database is authoritative for roles
and enabled/disabled state. No login-group write permission is required.

Three deployment methods — all produce equivalent results:
### Helm Chart (`deploy/helm/`)
```bash
helm install troshka deploy/helm/ -n troshka \
  --set postgres.deploy=true \
  --set auth.oauthEnabled=true \
  --set-string auth.initialAdmin=person@example.com \
  --set route.host=troshka.apps.cluster.example.com
```
- Full-stack chart: backend, frontend, **troshka-tunnel**, PostgreSQL, S4, OAuth proxy, RBAC, migration Job
- All components conditional via `values.yaml` toggles (`postgres.deploy`, `s4.deploy`, `auth.oauthEnabled`, `tunnel.enabled`)
- **troshka-tunnel**: dedicated Deployment/Service/Route/HPA for nested-OCP `troshka-oc` WebSockets (`app.tunnel_main:app`). Route host is `route.host` with `troshka.` → `troshka-tunnel.`. Isolates port-forward load from the API worker — see [`troshka-oc.md`](troshka-oc.md)
- Global `deploy: false` suppresses all resources (ArgoCD pattern)
- Migration runs as Helm post-install/post-upgrade hook
- Secrets auto-generated on first install, preserved on upgrade (`helm.sh/resource-policy: keep`)
- ConfigMap changes trigger pod restart via `checksum/config` annotation
- infra01's expanded S4 PVC is recorded in the non-secret
  `deploy/helm/values-infra01.yaml` override. Include it with `-f` for Helm, or
  reference it in Argo CD's Helm `valueFiles` once the file is on the configured
  Git revision. Supply secrets separately; do not commit private Argo values.
- S4 image selection is configurable through Helm `s4.image`, Ansible
  `troshka_s4_image`, or the standalone central-S4 playbook's `s4_image`. Pin a
  validated image digest when deploying the server fix; the existing upstream
  default is not a claim that its multipart bug is fixed. Use the same image for
  RGW and `radosgw-admin` initialization. Kustomize can override the image in its
  `images` section. All S4 deployment paths use `Recreate` for the single RWO PVC.
- **OAuth proxy has NO auto-rollout** — changing `--skip-auth-regex` or other args requires a manual `oc rollout restart deployment/troshka-oauth-proxy`. Do NOT add a checksum annotation to the OAuth proxy pod template — the template contains `randAlphaNum` for secrets, so any re-render regenerates the client-secret and breaks OAuth login.
- ArgoCD should ignore `/spec/finalizers` on the `troshka` Namespace only:
  Kubernetes retains its namespace finalizer even when Helm omits the field.
  See `deploy/argocd/application-helm.yaml.example`. For an existing Application,
  append this pointer to its Namespace rule, preserving other ignore rules and
  private Helm values. This changes drift detection, not namespace finalizers
  or OAuth credentials.
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
  -e troshka_initial_admin=person@example.com
```
- Variables in `deploy/ansible/inventory/group_vars/all.yaml`
- Task order: namespace → RBAC → secrets → PostgreSQL → S4 → backend → migration → frontend → OAuth
- Secrets auto-generated on first deploy, preserved on re-deploy
- Undeploy: `ansible-playbook deploy/ansible/undeploy.yaml`

### CDI importer CPU tuning on provider clusters

The measured conversion knee was approximately 2 CPUs: a fully populated 2 GiB
QCOW2 converted in 6.05 s at 2 CPUs, 5.88 s at 4, and 5.65 s at 10 (two runs per
limit). The earlier 750m baseline averaged 12.24 s. Larger disks and concurrent
imports require their own measurements; these are not guaranteed production rates.

`infra/ocpvirt-cdi-storage-resources.patch.yaml` records the recommended 2-CPU
limit using `HyperConverged.spec.resourceRequirements.storageWorkloads`. This is
an explicit, **cluster-wide** OpenShift Virtualization setting, including CDI
pods outside Troshka. It is not applied automatically by the Troshka chart or
operator. The patch preserves existing requests and memory settings, and does
not resize pods already running. Review its scope before applying:

```bash
oc --kubeconfig=$HOME/secrets/<provider-cluster>.kubeconfig patch \
  hyperconverged kubevirt-hyperconverged -n openshift-cnv --type=merge \
  --patch-file infra/ocpvirt-cdi-storage-resources.patch.yaml
```

See [temporary production hotpatching](production-hotpatching.md) for source
overlays, their removal after an image rollout, and safe in-place importer CPU
resizing. The current production importer was individually resized to 4 CPUs;
the cluster-wide default has not been changed.

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
- **image** — in-cluster rollout-restart of `troshka-backend` and `troshka-frontend` Deployments (patch `kubectl.kubernetes.io/restartedAt` annotation); the repull of the running tag (auto-detected, e.g. `latest` or `production`) picks up the new digest. Also rolls `troshka-worker` and `troshka-tunnel` when present — both run the **backend** image (`app.tunnel_main` for tunnel) and must stay in sync with the API. Returns `{"status": "rolling_out"}`. Requires the `troshka-self-update` Role/RoleBinding bound to the backend ServiceAccount, scoping `get`/`patch` to the named `troshka-backend`/`troshka-frontend`/`troshka-worker`/`troshka-tunnel` deployments plus `get`/`list` on `pods`, and the `POD_NAMESPACE` downward-API env var on the backend Deployment for own-namespace discovery (falls back to the SA namespace file, then `troshka`). Missing RBAC surfaces the k8s `403` to the admin rather than a false success.
- **dev** — spawns a detached `./dev-services.sh restart backend` (`Popen(start_new_session=True)` from repo root); returns `{"status": "restarting"}` before uvicorn is killed. Frontend hot-reloads independently.
### Frontend banner (`src/frontend/src/app/layout.tsx`)
- Admin-only (`user?.role === "admin"`), polls `/update/status` every 60s. When `up_to_date` is false, renders a dismissible info banner in the existing `backendDown` slot with an "Apply update" (image) / "Restart backend" (dev) button; shows "Update in progress" while `rolling_out`. Dismissal persists in `localStorage` keyed by target digest / process-change marker, so a newer update re-shows it.
