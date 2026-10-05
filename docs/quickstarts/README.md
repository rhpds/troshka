# Troshka Install Quickstarts

Three equal rails for installing the Troshka **control plane**. Each has a one-command install and a one-command teardown that destroys Troshka workloads first.

| Rail | Guide | Install |
|---|---|---|
| **OpenShift** | [ocp.md](ocp.md) | `./quickstarts/ocp/install.sh` |
| **Amazon EKS** | [eks.md](eks.md) | `./quickstarts/eks/install.sh` |
| **Local (Mac/Windows/Linux)** | [local.md](local.md) | `./quickstarts/local/install.sh` |

Full wipe (all platforms): [teardown.md](teardown.md)

## Control plane vs compute

| Layer | Meaning |
|---|---|
| **Control plane** | Troshka UI/API/DB (what these quickstarts install) |
| **Compute** | Where lab VMs run — local libvirt, remote Linux, or EC2 / Azure / GCP / KubeVirt |

After any install, pick compute from the menu in that rail’s doc. Linux local installs can use cloud providers too.

## Metering / chargeback (quickstart hosts)

Quickstart installs are admin platforms. Compute hosts default to **shared** billing:

- Nested guests (ocpvirt / AWS / GCP / Azure / libvirt) pay for **running** VM CPU/RAM at catalog rates × `metering.nested_factor` (default **0.5**), so overcommit is cheaper than a same-size public-cloud VM.
- **KubeVirt** shared-cluster hosts skip the nested factor (full unit rates).
- Project **stop** freezes the meter (no further accrual until start).
- Mark a host **dedicated** in Admin → Hosts → Metering only for single-tenant CI that should charge the **full host hourly** whether VMs are running or not.
- Metering tabs: **Active** (live spend), **Past** (per-project bill on destroy), **Invoices** (per-owner monthly rollup, UTC month).

## Deep guides

- [OpenShift (SSO, overlays)](../install-ocp.md)
- [AWS provider](../install-aws.md) · [GCP](../install-gcp.md) · [Azure](../install-azure.md) · [OCP Virt](../install-ocpvirt.md) · [KubeVirt](../install-kubevirt.md)
