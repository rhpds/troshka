# Troshka Installation Guide: KubeVirt Native Provider

This guide covers preparing OCP clusters for the KubeVirt native provider, which creates VMs directly on OpenShift Virtualization — no nested virtualization or host VMs needed.

For deploying the Troshka application itself on OCP, see [OpenShift Deployment](install-ocp.md). For the nested OCP Virt provider (VMs inside KubeVirt VMs), see [OCP Virt Provider](install-ocpvirt.md).

## Overview

The KubeVirt native provider creates VMs as KubeVirt `VirtualMachine` CRs on target OCP clusters. A kopf-based operator manages custom CRDs (`TroshkaProject`, `TroshkaNetwork`, `TroshkaVM`) that reconcile into KubeVirt VMs, OVN secondary networks, and helper pods (dnsmasq, gateway).

**Architecture**: Troshka backend → Kubernetes API → TroshkaProject CR → Operator → KubeVirt VMs + networking pods

## Prerequisites

Each target cluster needs:

- **OpenShift 4.14+** with OpenShift Virtualization (KubeVirt + CDI)
- **ODF** (OpenShift Data Foundation) with Ceph RBD and CephFS storage
- **OVN-Kubernetes** for secondary network support (NetworkAttachmentDefinitions)
- **Cluster admin access** for initial RBAC/SCC setup (one-time)

## Cluster Preparation

All steps below must be run by a cluster admin. The setup script at `~/secrets/troshka-shared-ocpv/setup-ocpv-providers.sh` automates steps 1-4 — run it with admin kubeconfigs for each cluster.

### Step 1: Apply Provider RBAC

The `troshka` ServiceAccount needs a ClusterRole with permissions for KubeVirt VMs, CDI DataVolumes, Routes, PVCs, Secrets, Namespaces, Nodes, NetworkAttachmentDefinitions, VolumeSnapshots, and more.

```bash
oc apply -f infra/ocpvirt-rbac.yaml
```

This creates:
- **Namespace** `troshka`
- **ServiceAccount** `troshka` in the `troshka` namespace
- **ClusterRole** `troshka-provider` with ~20 API group rules
- **ClusterRoleBinding** `troshka-provider`
- **SCC** `troshka-virt-exec` (for exec into virt-launcher pods)
- **SCC** `troshka-privileged-jobs` (for recert/guestfish Jobs)

### Step 2: Apply Operator RBAC

The operator's ClusterRole must be pre-applied by a cluster admin — K8s prevents ServiceAccounts from creating ClusterRoles with permissions they don't hold (RBAC escalation prevention).

```bash
oc apply -f src/operator/deploy/clusterrole.yaml
oc apply -f src/operator/deploy/clusterrolebinding.yaml
```

This creates:
- **ClusterRole** `troshka-operator` — permissions for Troshka CRDs, KubeVirt VMs, CDI, OVN NADs, kopf peering, and per-namespace RBAC/SCC management
- **ClusterRoleBinding** `troshka-operator` — binds the operator SA

### Step 3: Apply Network SCCs

The operator creates dnsmasq and gateway pods that need `NET_ADMIN` and `NET_RAW` capabilities. The SCCs must exist before the operator tries to create these pods.

```bash
oc apply -f src/operator/deploy/scc.yaml
```

This creates:
- **SCC** `troshka-network-pods` — NET_ADMIN, NET_RAW for dnsmasq/DHCP pods
- **SCC** `troshka-gateway` — NET_ADMIN, NET_RAW for gateway/NAT pods
- Updates `troshka-virt-exec` and `troshka-privileged-jobs` SCCs

### Step 4: Create Service Account Token

Create a long-lived token (1 year) for the Troshka backend to authenticate to the cluster:

```bash
oc create token troshka -n troshka --duration=8760h
```

Save this token — it's used when registering the provider with the Troshka API.

### Step 5: Register Provider

Register the cluster as a KubeVirt native provider via the Troshka API or UI:

**UI**: Admin → Providers → Add Provider → Type: KubeVirt Native → enter API URL and token

**API**:
```bash
curl -X POST https://troshka-api.apps.cluster.example.com/api/v1/providers/ \
  -H "Authorization: Bearer $TROSHKA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "ocpv01",
    "type": "kubevirt",
    "api_url": "https://api.ocpv01.example.com:6443",
    "token": "<token-from-step-4>",
    "verify_ssl": false
  }'
```

On creation, the backend automatically:
1. Deploys the Troshka operator (CRDs + Deployment) to the cluster
2. Queries cluster capacity (worker node vCPUs, RAM, Ceph storage)
3. Creates a virtual Host record representing the cluster

## Automated Setup Script

For bulk setup across multiple clusters, use the setup script:

```bash
# Configure
echo 'TROSHKA_API_KEY=trk_...' > ~/secrets/troshka-shared-ocpv/.env

# Run (applies RBAC, creates tokens, registers providers)
~/secrets/troshka-shared-ocpv/setup-ocpv-providers.sh

# Dry run first
~/secrets/troshka-shared-ocpv/setup-ocpv-providers.sh --dry-run
```

The script requires admin kubeconfigs in `~/secrets/` for each cluster. It:
1. Applies `infra/ocpvirt-rbac.yaml` (provider SA + ClusterRole)
2. Applies operator ClusterRole, ClusterRoleBinding, and SCCs
3. Creates a 1-year SA token
4. Saves a troshka-specific kubeconfig to `~/secrets/troshka-shared-ocpv/`
5. Registers the provider via the Troshka API
6. Creates a read-only S3 provider for central gold images

## Verify

After setup, verify on each cluster:

```bash
# RBAC
oc auth can-i create virtualmachines --as=system:serviceaccount:troshka:troshka
# Expected: yes

# Operator running
oc get deployment troshka-operator -n troshka
# Expected: 1/1 READY

# CRDs installed
oc get crd | grep troshka
# Expected: troshkaprojects, troshkanetworks, troshkavms

# SCCs exist
oc get scc | grep troshka
# Expected: troshka-network-pods, troshka-gateway, troshka-virt-exec, troshka-privileged-jobs
```

In the Troshka UI:
- Admin → Hosts should show the cluster as active with capacity (vCPUs, RAM, storage)
- Create a test project and deploy to verify end-to-end

## Differences from Nested OCP Virt Provider

| Feature | Nested OCP Virt (`ocpvirt`) | KubeVirt Native (`kubevirt`) |
|---------|---------------------------|------------------------------|
| Architecture | Host VM on KubeVirt, troshkad inside | VMs directly on KubeVirt via operator |
| Nested virt | Required | Not needed |
| Host agent | troshkad daemon in host VM | No agent — operator manages VMs |
| Networking | VXLAN + nftables inside host VM | OVN layer2 secondary networks + dnsmasq/gateway pods |
| Storage | Ceph-NFS mount inside host VM | Ceph RBD PVCs via CDI DataVolumes |
| Console | vncd in host VM | VNC proxy pod per project |
| BMC | sushy/vbmc in host VM | sushy pod with KubeVirt driver (Redfish only) |
| Live migration | libvirt TLS between pool hosts | KubeVirt native live migration (future) |
| Clock backdating | `virsh domtime` | Not supported |
| External access | OCP Routes for port forwards | OCP Routes for port forwards |

## Troubleshooting

### Operator not starting

```bash
oc logs deployment/troshka-operator -n troshka
```

Common causes:
- **403 Forbidden on CRDs** — operator ClusterRole not applied (Step 2)
- **ClusterRoleBinding namespace mismatch** — the binding subjects must reference the correct namespace

### Pods failing with SCC errors

```bash
oc get events -n troshka-<project-id> --sort-by='.lastTimestamp'
```

Common causes:
- **`troshka-network-pods` SCC missing** — run Step 3
- **SA not in SCC users list** — the operator patches this automatically, but the SCC must exist first

### Deploy stuck at "images: waiting"

The operator is downloading disk images from S3 via CDI DataVolumes. Check:

```bash
oc get datavolumes -n troshka-<project-id>
oc get pods -n troshka-<project-id> | grep importer
oc logs <importer-pod> -n troshka-<project-id>
```

Common causes:
- S3 endpoint not reachable from the cluster
- S3 credentials incorrect
- CDI not installed or misconfigured
