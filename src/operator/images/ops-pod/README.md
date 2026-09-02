# troshka-ops-pod execution-environment image

Execution-environment (EE) image for the Troshka **ops pod** (multi-cluster OCP,
bastionless install — Plan 4). The ops pod runs the OpenShift agent-based install
for one or more clusters from *inside* the project network, so a bastion VM is no
longer required.

## Contents

| Tool | Purpose |
|------|---------|
| `oc` / `kubectl` | cluster access post-install |
| `openshift-install` | `agent create image`, `agent wait-for` |
| `ansible-core` + `ansible-navigator` | run the `troshka.cloud` collection |
| `troshka.cloud` collection | Troshka API modules (from the separate collection repo) |
| `curl` + `jq` | Redfish virtual-media / power control against node BMCs |
| `python3` | serves the generated agent ISO over `http.server` |
| `bind-utils` (`dig`/`nslookup`) | DNS checks during install |

Base: `registry.access.redhat.com/ubi9/python-311` (matches the other Troshka
python images: `bmc`, `vnc-proxy`, backend).

## Build (CI only)

**Do NOT build this locally** — per project convention (`CLAUDE.md`) all images
are built and pushed by CI. The GitHub Actions `build-operator` job in
[`.github/workflows/build-images.yml`](../../../../.github/workflows/build-images.yml)
builds and pushes:

```
quay.io/redhat-gpte/troshka-ops-pod:<sha>
quay.io/redhat-gpte/troshka-ops-pod:latest
```

CI builds the image tracking the latest `stable` OpenShift client/installer
(tagged `:latest`); the version can be overridden with a build arg:

```bash
podman build \
  -f src/operator/images/ops-pod/Dockerfile \
  --build-arg OCP_VERSION=stable \
  -t quay.io/redhat-gpte/troshka-ops-pod:latest \
  src/operator/images/ops-pod/
```

Build args:

- `OCP_VERSION` — OCP client/installer version baked in (default `stable`; CI
  builds the `:latest` image tracking `stable`, but you can pass an exact
  `x.y.z` for a pinned, reproducible image). The install runner may still
  re-download a per-project version at runtime, so this is a self-sufficiency
  default, not a hard pin.
- `OCP_MIRROR` — mirror base URL (default `mirror.openshift.com`).
- `TROSHKA_COLLECTION_GIT` — source for the `troshka.cloud` collection (default
  the git repo). Replace with a Galaxy/Automation-Hub ref once published there.

## Config wiring

The backend resolves the image ref from config key **`ocp.ops_pod_image`**
(`src/backend/config/config.yaml`), overridable via
`TROSHKA_OCP__OPS_POD_IMAGE`. When empty, `ops_pod_scaffold._resolve_ops_pod_image()`
falls back to the baked default `quay.io/redhat-gpte/troshka-ops-pod:latest`.

## TODO before the live run works

- The image must be **published** to `quay.io/redhat-gpte/troshka-ops-pod`
  before an ops-pod install can run (the config default points there).
- If/when the `troshka.cloud` collection is published to Galaxy / Automation Hub,
  switch `TROSHKA_COLLECTION_GIT` to a versioned collection ref for reproducible,
  offline-friendly builds.
