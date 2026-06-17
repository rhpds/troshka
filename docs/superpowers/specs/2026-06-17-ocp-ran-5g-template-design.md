# OCP 5G RAN Lab Template Design

## Overview

Add a new Troshka OCP template (`ocp-ran-5g`) that deploys the infrastructure needed to run the [5G RAN RDS Deployments on OpenShift](https://github.com/RHsyseng/5g-ran-deployments-on-ocp-lab) lab. This replaces the existing Babylon/AgnosticV CI (`openshift_cnv/OCP4-RAN-CNV`) which relies on a golden bastion image running nested kcli/libvirt.

Instead of nesting everything inside one massive bastion VM, Troshka provides the VMs and BMC (Redfish via sushy-emulator) natively, and ACM/ZTP deploys the SNO clusters onto blank VMs — the same way it would on real bare-metal hardware.

### What the Lab Teaches

The lab is **not** about SR-IOV or PTP hardware. It teaches the **full RAN deployment lifecycle at scale**:

- ZTP (Zero Touch Provisioning) — deploying SNO clusters via ACM using Agent-Based Install and Image-Based Install
- GitOps policy management — ArgoCD + PolicyGenerator for telco RAN configurations as code
- Fleet lifecycle management — TALM for rolling upgrades, LCA for image-based upgrades
- RAN Reference Design Specification — crafting the full telco profile via policy templates

Students deploy 2 of the 3 SNO clusters themselves during the lab exercises. The third (seed SNO) is pre-deployed by automation so students can generate an IBI seed image from it.

## Topology

### VMs

| VM | Role | vCPU | RAM (GB) | Disk (GB) | OS | BMC | Power | Notes |
|----|------|------|----------|-----------|-----|-----|-------|-------|
| hub-cp-0 | Hub OCP control plane (SNO mode) | 16 | 48 | 120+120 | RHCOS | Yes | On at deploy | Agent Installer via Troshka. Second disk for LVMS. |
| hub-cp-1 | Hub OCP control plane (compact mode) | 16 | 26 | 120+120 | RHCOS | Yes | On at deploy | Only in compact mode. |
| hub-cp-2 | Hub OCP control plane (compact mode) | 16 | 26 | 120+120 | RHCOS | Yes | On at deploy | Only in compact mode. |
| bastion | Lab services + student terminal | 4 | 8 | 100 | RHEL 10 | No | On at deploy | Gitea, MinIO, registry, dnsmasq, Showroom |
| sno-seed | Seed SNO cluster | 12 | 24 | 200+200 | Blank | Yes | Off at deploy | ACM deploys via ZTP. Second disk for LVMS. |
| sno-abi | Student-deployed ABI SNO | 12 | 24 | 200+200 | Blank | Yes | Off at deploy | Student deploys in lab section 20 |
| sno-ibi | Student-deployed IBI SNO | 12 | 24 | 200+200 | Blank | Yes | Off at deploy | Student deploys in lab section 22 |

**Hub mode toggle:** The template has a `hub_mode` parameter:
- `sno` (default): 1 hub node (hub-cp-0 only), MultiClusterHub with `availabilityConfig: Basic`
- `compact`: 3 hub nodes (hub-cp-0/1/2), MultiClusterHub with `availabilityConfig: High`

**Resource totals:**
- SNO hub mode: 56 vCPU, 128 GB RAM, ~1.56 TB disk (16+4+12+12+12 vCPU, 48+8+24+24+24 GB)
- Compact hub mode: 88 vCPU, 180 GB RAM, ~1.8 TB disk (16+16+16+4+12+12+12 vCPU, 48+26+26+8+24+24+24 GB)

### Networks

| Network | CIDR | DHCP | DNS | Purpose |
|---------|------|------|-----|---------|
| cluster | 192.168.125.0/24 | Yes | Yes (5g-deployment.lab) | Primary cluster network. All VMs. Hub API/Ingress VIPs. |
| bmc | 192.168.50.0/24 | No | No | BMC/Redfish access. Bastion + hub nodes + SNO VMs. |
| sriov | 192.168.100.0/24 | No | No | SR-IOV emulation network. SNO VMs only (igb NICs). |
| ptp | 192.168.200.0/24 | No | No | PTP emulation network. Hub nodes + SNO VMs (igb NICs). |
| gateway | — | — | — | NAT outbound for internet access (registry pulls, OCP install) |

Note: CIDRs for cluster, SR-IOV, and PTP networks match the original lab documentation exactly. The BMC network uses 192.168.50.0/24 (Troshka internal, not referenced in lab docs) to avoid collision with the lab's SR-IOV CIDR (192.168.100.0/24).

### NIC Assignments

| VM | NIC 0 (cluster) | NIC 1 (bmc) | NIC 2 (ptp) | NIC 3 (sriov) | NIC 4 (sriov) |
|----|-----------------|-------------|-------------|---------------|---------------|
| bastion | virtio | virtio | — | — | — |
| hub-cp-* | virtio | virtio | igb | — | — |
| sno-seed | virtio | — | igb | igb | igb |
| sno-abi | virtio | — | igb | igb | igb |
| sno-ibi | virtio | — | igb | igb | igb |

SNO VMs need 2 SR-IOV NICs (one for netdevice VFs, one for vfio-pci VFs) plus 1 PTP NIC, matching the existing lab's kcli config. Hub nodes get PTP NICs so they can run PTP operator tests.

### DNS Records (on cluster network)

These are required for the hub cluster. Created on the cluster network node:

| Record | IP | Purpose |
|--------|-----|---------|
| `api.hub.5g-deployment.lab` | Hub API VIP (192.168.125.10) or SNO IP | OCP API |
| `api-int.hub.5g-deployment.lab` | Same | Internal API |
| `*.apps.hub.5g-deployment.lab` | Hub Ingress VIP (192.168.125.11) or SNO IP | Routes/Ingress |
| `infra.5g-deployment.lab` | Bastion IP (192.168.125.50) | Lab services (Gitea, registry, etc.) |

SNO cluster DNS records are managed by ACM during ZTP provisioning, not pre-configured.

## NIC Model Support (New Feature)

### Current State

Troshka hardcodes `"model": "virtio"` for all NICs in `deploy_service.py:1130`. The NIC `model` field exists in the topology schema but is never read.

### Change

Read `nic.model` from topology data. Supported values: `virtio` (default), `e1000e`, `igb`.

The `igb` model emulates the Intel 82576, which supports SR-IOV virtual function emulation in QEMU. This is what the SR-IOV operator discovers — vendor `8086`, device `10c9`. With `DEV_MODE: "TRUE"` on the operator subscription, it accepts these emulated NICs and creates VFs from them.

### Files Changed

- `src/backend/app/services/deploy_service.py` — read `nic.model` instead of hardcoding `"virtio"`
- `src/troshkad/troshkad.py` — pass model to virt-install `--network` args (e.g., `--network bridge=br-xxx,model=igb`)
- Frontend NIC editor — add model dropdown (virtio/e1000e/igb) to the NIC properties panel

## Template Definition

### `src/backend/templates/ocp-ran-5g.yaml`

```yaml
name: ocp-ran-5g
display_name: "5G RAN Lab (ACM + ZTP + GitOps)"
description: "Hub cluster + 3 blank SNO targets for 5G RAN RDS Deployments lab"
category: openshift
install_method: agent
deploy_time: "~45 min (hub install) + ~60 min (seed SNO via ACM)"
extends: ocp-cluster
defaults:
  hub_mode: sno           # "sno" or "compact"
  control_vcpus: 16
  control_ram_gb: 48
  control_disk_gb: 120
  control_schedulable: true
  worker_count: 0
  sno_count: 3
  sno_vcpus: 12
  sno_ram_gb: 24
  sno_disk_gb: 200
  bastion_vcpus: 4
  bastion_ram_gb: 8
  bastion_disk_gb: 100
  ocp_version: "4.20"
  disconnected: true        # Use local registry mirror
```

### Template Loader Changes

`template_loader.py` needs to handle the `ocp-ran-5g` template:

1. Generate hub nodes based on `hub_mode` (1 or 3 nodes)
2. Generate 3 blank SNO VMs with BMC enabled, powered off, extra igb NICs
3. Generate 4 networks (cluster, bmc, sriov, ptp) + gateway
4. Wire edges between NICs and networks
5. Add extra 120GB/200GB disks to hub/SNO nodes for LVMS

### OCP Customization (`ocp/agent_template.py`)

The existing `customize_topology()` handles DNS records, bastion cloud-init, install-config.yaml, and agent-config.yaml for the hub cluster. For the RAN template, additional customization is needed:

- Set the cluster domain to `5g-deployment.lab` (not `ocp.local`)
- Set cluster name to `hub`
- Configure hub API/Ingress VIPs on the cluster network
- Add bastion cloud-init for all lab services (see below)

## Post-Install Automation

After the hub OCP cluster is installed by Troshka's Agent Installer, cloud-init scripts on the bastion handle the remaining setup. This runs as `runcmd` steps in the bastion's cloud-init user-data.

**Pattern-based deployment model:** Phases 2-4 run only on the **initial build**. Once the environment is fully configured, it's saved as a Troshka pattern. Subsequent deploys from the pattern restore the entire state — all operators, the seed SNO, and configurations come back intact. Only Phase 1 (bastion services) needs to run on every deploy since bastion state is not captured in patterns (it's cloud-init driven).

### Phase 1: Bastion Services (runs every deploy)

These run immediately on bastion boot (no dependency on hub cluster):

1. **Container registry** — podman container serving on port 8443, TLS, htpasswd auth
2. **Gitea** — git server on port 3000, pre-loaded with the lab repo
3. **MinIO** — S3-compatible storage on port 9002, pre-created buckets (sno-abi, sno-ibi, logs, multiclusterobservability)
4. **dnsmasq** — DNS/DHCP for the lab network, records for hub/SNO clusters and infra services
5. **Showroom** — lab guide web UI (Apache + Wetty terminal + Traefik reverse proxy)
6. **Webcache** — HTTP server for RHCOS live ISO and rootfs images

### Phase 2: Operator Mirroring (initial build only)

Mirror required operator images to the local registry using `oc-mirror`. This is needed because the lab runs in disconnected mode. Operators to mirror:

- advanced-cluster-management
- multicluster-engine
- openshift-gitops-operator
- lvms-operator
- sriov-network-operator (with `DEV_MODE: "TRUE"`)
- lifecycle-agent
- redhat-oadp-operator
- ptp-operator
- cluster-logging
- topology-aware-lifecycle-manager

### Phase 3: Hub Cluster Configuration (initial build only)

After Troshka confirms OCP install is complete:

1. Install ACM operator from the local registry catalog source
2. Create MultiClusterHub CR (with `availabilityConfig` based on hub_mode)
3. Wait for MCH/MCE to become Ready
4. Install and configure ArgoCD (openshift-gitops-operator)
5. Install LVMS operator, create LVMCluster, set as default storage class
6. Configure ArgoCD for ZTP support (kustomize plugins, RBAC)
7. Create admin/developer users with generated passwords
8. Configure MultiClusterObservability (optional, connects to MinIO)

### Phase 4: Seed SNO Deployment (initial build only)

1. Create BareMetalHost CRDs for `sno-seed` pointing at Troshka's sushy-emulator:
   - BMC address: `redfish-virtualmedia://<bastion-bmc-ip>:8000/redfish/v1/Systems/<domain-name>`
   - Note: Troshka's sushy uses the libvirt domain name as the system ID, and listens on port 8000 on the BMC network. The BareMetalHost CRD BMC address must be reachable from the hub pods via the hub's BMC NIC.
2. Create InfraEnv, ClusterDeployment, AgentClusterInstall for sno-seed
3. Wait for ACM to discover the host, boot it via Redfish virtual media, install RHCOS, and complete the SNO install
4. Install RAN operators on the seed SNO (SR-IOV, PTP, LVMS, OADP, LCA, logging)
5. Apply performance profile and tuned patches
6. Create BareMetalHost CRDs for `sno-abi` and `sno-ibi` (so ACM knows about them, but doesn't deploy yet — students trigger that)

### BMC Address Mapping

Key integration point: the BareMetalHost CRD needs to reference Troshka's sushy-emulator correctly.

Troshka's sushy-emulator:
- Listens on port 8000 on each host
- Uses libvirt domain names as system IDs: `troshka-{project_id[:8]}-{vm_id[:8]}`
- Accessible on the BMC network at the host's BMC IP

The BareMetalHost CRD would use:
```
redfish-virtualmedia://<host-bmc-ip>:8000/redfish/v1/Systems/<domain-name>
```

The post-install automation needs to discover the correct domain names and host BMC IP. This can be done by:
1. Reading the deployed topology from the Troshka API
2. Constructing the domain name from project_id and vm_id
3. Using the host's BMC-network IP (known from the host model)

This mapping logic lives in the bastion's cloud-init script or a helper script that queries the Troshka API.

## AgnosticV CI Configuration

The cloned CI at `~/agnosticv/troshka/` needs to be adapted to point at Troshka instead of Babylon/kcli. The key changes:

### common.yaml

- Remove `cloud_provider: openshift_cnv` (no longer deploying on OCP Virt via Babylon)
- Remove `instances` block (Troshka manages VMs, not agnosticd)
- Remove `env_type: base-infra` (not using agnosticd's infra provisioning)
- Keep `__meta__` catalog metadata, workload references, and owner info
- Add Troshka-specific variables (project template, hub_mode, etc.)

### Workload Adaptation

The existing workloads (`ocp4_workload_external_odf`, `ocp4_workload_5gran_deployments_lab`) assume they're running on a kcli-managed bastion. They need adaptation:

- **pre_workload.yml**: Skip kcli-specific tasks (network creation, VM creation, sushy setup). These are already done by Troshka. Keep: dependency installation, dnsmasq config, registry setup, Gitea setup, Showroom setup, pull secret handling.
- **workload.yml**: Replace `kcli create cluster openshift --pf hub.yml` with waiting for Troshka's OCP install. Keep: all ACM/ArgoCD/operator installation, seed SNO deployment via BareMetalHost CRDs.
- Update BMC addresses from `192.168.125.1:9000/redfish/v1/Systems/local/sno-seed` to Troshka's format.

This workload adaptation can be handled incrementally — the cloud-init approach embeds the essential steps directly, and full agnosticd integration comes later.

## Template Parameters (Frontend)

When creating a project from the `ocp-ran-5g` template, the user sees:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| Hub Mode | Dropdown (sno/compact) | sno | SNO for testing, compact for production-like |
| OCP Version | Dropdown | 4.20 | Hub and SNO cluster version |
| Bastion Password | Password | (required) | Student/cloud-user password |
| Pull Secret | (from user settings) | — | Red Hat pull secret for OCP install |
| SSH Key | (from user settings) | — | SSH public key |
| Disconnected | Toggle | true | Use local registry mirror |

## What Troshka Already Handles (No Changes Needed)

- VM lifecycle (create, start, stop, destroy)
- BMC/Redfish via sushy-emulator (virtual media boot, power control)
- Multiple VXLAN networks with isolation
- DNS on network nodes (dnsmasq)
- Cloud-init for bastion configuration
- Agent-Based Installer for the hub OCP cluster
- Gateway with NAT for outbound internet access
- External access / port forwarding for student SSH/web access
- VNC console for visual access to VMs

## Implementation Order

1. **NIC model support** — small backend change, read model from topology
2. **Template YAML** — `ocp-ran-5g.yaml` template definition
3. **Template loader** — generate the RAN topology (hub + SNOs + bastion + networks)
4. **Agent template customization** — RAN-specific DNS, cloud-init for lab services
5. **Post-install scripts** — ACM installation, operator mirroring, seed SNO deployment
6. **AgnosticV CI adaptation** — update `~/agnosticv/troshka/` config files
7. **Frontend** — NIC model dropdown, RAN template in new-project dialog
8. **Testing** — deploy on dev cluster, verify full lab flow

## Resolved Design Decisions

1. **Operator mirroring / deployment speed**: The first deploy installs all operators (ACM, ArgoCD, LVMS, SR-IOV, PTP, etc.) and completes the full setup. This fully-configured environment is then saved as a **Troshka pattern**. Subsequent deploys restore from the pattern — no operator installation or mirroring needed. This is the standard Troshka workflow: build once, pattern-save, instant redeploy.

2. **Showroom deployment**: Runs on the bastion via cloud-init as podman containers. The Showroom setup should be implemented as a **modular cloud-init snippet** (reusable function/template) since Showroom will be needed for future lab templates beyond the RAN lab. The snippet takes parameters: lab repo URL, lab version, student credentials, bastion hostname.

3. **Hub cluster domain**: Hardcoded to `5g-deployment.lab` to match the lab documentation exactly. No configurability needed — changing the domain would break the lab instructions.

4. **SNO MAC addresses**: Template the BareMetalHost CRDs and dnsmasq configs with the actual MACs from the deployed topology. The post-install script queries the Troshka API for the deployed topology, extracts the MAC addresses, and generates the CRDs and dnsmasq entries dynamically. This is more robust than forcing fixed MACs and is consistent with how Troshka handles other topology-dependent configuration.
