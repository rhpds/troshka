# Troshka Quickstart — Local (Mac / Windows / Linux)

Install the Troshka **control plane** with one Compose command on your laptop. Then choose where lab VMs run (local host, remote Linux, or a cloud provider).

## Prerequisites

- [Podman](https://podman.io/) or [Docker](https://docs.docker.com/) with Compose v2
- `curl` and `jq` (for install wait / teardown wipe)
- ~4 GB RAM free for the stack

## Install

From the repo root:

```bash
./quickstarts/local/install.sh
```

This starts Postgres, Redis, MinIO, backend, worker, and frontend, then prints:

- UI: `http://localhost:3100`
- API: `http://localhost:8200`

Dev auth is on (auto-admin). Secrets are generated once in `deploy/compose/.env`.

## Compute (where lab VMs run)

Troshka needs compute after the UI is up. Pick any:

### 1. Local / near-local libvirt host

| OS | Path |
|---|---|
| **Linux** | `./quickstarts/local/bootstrap-host.sh` then add the host in the UI and install the agent |
| **macOS** | `brew install libvirt qemu`, create a **Linux guest**, run `bootstrap-host.sh` **inside the guest** (troshkad does not run on Darwin) |
| **Windows** | WSL2 + nested virtualization + libvirt; run `bootstrap-host.sh` inside WSL |

Nested virtualization can fail on Apple Silicon or some Windows setups. If it does, use option 2 or 3.

### 2. Remote Linux

Any SSH-reachable Linux box with KVM — add it as a host in the UI and install the agent.

### 3. Cloud / virt provider

Configure EC2, Azure, GCP, or KubeVirt using the existing guides:

- [AWS](../install-aws.md) · [GCP](../install-gcp.md) · [Azure](../install-azure.md) · [KubeVirt](../install-kubevirt.md) · [OCP Virt](../install-ocpvirt.md)

## Teardown

```bash
./quickstarts/local/teardown.sh
# non-interactive:
./quickstarts/local/teardown.sh --yes
```

This destroys all projects via the API (VMs and artifacts on attached hosts/providers), verifies none remain, then `compose down -v`.

Full wipe details: [teardown.md](teardown.md).
