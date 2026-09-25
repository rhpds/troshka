# Troshka Quickstart — OpenShift

Deploy the Troshka **control plane** on an OpenShift 4.x cluster with one Helm command.

For SSO, groups, and advanced options see the deep guide: [install-ocp.md](../install-ocp.md).

## Prerequisites

- OpenShift 4.x with `oc` (or `kubectl`) and cluster-admin (or enough rights to create a namespace, Deployments, Routes, PVCs, and RBAC)
- [Helm 3](https://helm.sh/)
- `curl` and `jq` (teardown wipe)

## Install

```bash
./quickstarts/ocp/install.sh
```

Defaults:

- Namespace / release: `troshka`
- In-cluster Postgres + S4 + Redis
- OAuth **off** (dev auto-admin) for the blog happy path
- Image tag: `latest` (override with `TROSHKA_IMAGE_TAG`)

The script prints the Route URL when ready.

## Compute (where lab VMs run)

Same menu as every rail:

1. Local/near-local libvirt host (or WSL / macOS Linux guest — see [local.md](local.md))
2. Remote Linux + troshkad
3. EC2 / Azure / GCP / [KubeVirt](../install-kubevirt.md) / [OCP Virt](../install-ocpvirt.md)

## Teardown

```bash
./quickstarts/ocp/teardown.sh
./quickstarts/ocp/teardown.sh --yes
```

Destroys all projects via the API, verifies clean, then `helm uninstall` and deletes the namespace.

Details: [teardown.md](teardown.md).
