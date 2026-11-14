# Argo bootstrap

Requires `oc`, Helm, Git, Python 3 with PyYAML, cluster access, and an existing
Argo CD installation. On OpenShift, `--dev` uses the installed OpenShift GitOps
operator's `spec.imageUpdater.enabled` capability and the ImageUpdater CRD.
It does not install the GitOps operator. The default Argo namespace and instance
are `openshift-gitops`.

Prepare an existing dev Helm installation for adoption:

```bash
python3 scripts/bootstrap_argocd.py \
  --kubeconfig="$HOME/secrets/ocpvdev01.dal13.infra.demo.redhat.com.kubeconfig" \
  --name troshka-dev --namespace troshka-dev \
  --revision main --dev --adopt \
  --output /private/tmp/troshka-dev-argo-bootstrap
```

Review `review.json`. The other bundle files may contain live credentials and
must remain private. The directory is created with private permissions. For a
new installation, omit `--adopt` and supply a complete `--values` file containing
non-secret configuration. Use a named `v*` revision without `--dev` for a fixed
release. The referenced commit must exist locally and in the published repo.
Fresh dev installations enable automatic sync and self-heal without pruning.
Existing installations retain manual sync during adoption.

For adoption, scheduler settings come from the backend's mounted config, not the
worker-only config. Explicit quota-enforcement environment overrides are carried
into Helm values; conflicting overrides stop preparation for review. This avoids
silently disabling collection or enabling enforcement while replacing overlays.

```bash
python3 scripts/bootstrap_argocd.py --apply --sync \
  --output /private/tmp/troshka-dev-argo-bootstrap
python3 scripts/bootstrap_argocd.py --verify \
  --output /private/tmp/troshka-dev-argo-bootstrap
```

The dev Application follows `main`. Backend and frontend images watch Quay's
`latest` tag; the exact Application's ImageUpdater uses digest tracking and
Argo write-back. Database, Redis, S4 and OAuth images retain their independent
versions. The separately deployed Troshka operator is not managed by this chart.

## Existing installation handoff

Adoption uses selective sync, excludes migration/initialization hooks, disables
pruning and automatic sync, and protects existing credentials, pod templates,
replica counts and ConfigMap data. It does not stop VMs or replace databases.
Those temporary guards intentionally prevent an image rollout, including one
requested by ImageUpdater. `Synced/Healthy` at this stage confirms adoption,
not that the running containers use `latest`.

Before enabling automatic sync, capture dev source overlays and configuration
in supported chart values or published images, confirm worker jobs can finish,
review the rendered diff, and remove only the corresponding adoption guards.
Then validate the first image rollout and enable self-heal without pruning.
Do not remove all guards to force an update: that can discard dev fixes.

An interrupted apply is never retried automatically. Inspect the live objects
and the private bundle first. If the Application was created with the exact
prepared spec and no operation is pending, `--resume-sync --output DIR` rechecks
saved resource identities/content and submits only the pending sync. It does
not recreate resources or retry image-updater setup. Later Application edits
require a newly reviewed plan, not reuse of that interrupted bootstrap bundle.

On OpenShift, always address `applications.argoproj.io` explicitly; the short
name `Application` can resolve to another installed API.
