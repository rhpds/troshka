# Troshka Installation Guide: KubeVirt Native Provider

This guide covers preparing OCP clusters for the KubeVirt native provider, which creates VMs directly on OpenShift Virtualization — no nested virtualization or host VMs needed.

For deploying the Troshka application itself on OCP, see [OpenShift Deployment](install-ocp.md). For the nested OCP Virt provider (VMs inside KubeVirt VMs), see [OCP Virt Provider](install-ocpvirt.md).

## Overview

The KubeVirt native provider creates VMs as KubeVirt `VirtualMachine` CRs on target OCP clusters. A kopf-based operator manages custom CRDs (`TroshkaProject`, `TroshkaNetwork`, `TroshkaVM`) that reconcile into KubeVirt VMs, OVN secondary networks, and helper pods (dnsmasq, gateway).

**Architecture**: Troshka backend → Kubernetes API → TroshkaProject CR → Operator → KubeVirt VMs + networking pods

**Disk images**: Downloaded from S3 via CDI `source.s3` with `secretRef` for authenticated access. Supports dual S3 sources — a local read-write S4 for user content and a central read-only S4 for shared gold images. Images are cached as golden PVCs in a `troshka-cache` namespace and cloned per-project via CDI.

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

## S3 Storage Setup

KubeVirt native uses CDI `source.s3` for authenticated disk image downloads. Two S3 providers are needed:

### Read-Write S3 (local)

For user-created patterns, library uploads, and snapshots. Created as an S3 provider in the Troshka UI.

**Important**: The S3 endpoint must be reachable from all target clusters. For multi-cluster deployments, use an OCP Route with edge TLS termination (the Helm chart creates this automatically).

**S4 Credentials**: The S4 image initializes with default credentials (`s4admin`/`s4secret`). For production, create a custom user with a random password via `radosgw-admin` inside the S4 pod:

```bash
# Generate a random key
NEW_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

# Create user inside S4 pod
oc exec deployment/troshka-s4 -n troshka -- \
  radosgw-admin user create --uid=troshka --display-name="Troshka" \
  --access-key=troshka --secret-key="$NEW_KEY"

# Grant bucket access via policy (using old credentials)
python3 -c "
import boto3, json
client = boto3.client('s3', endpoint_url='http://troshka-s4:7480',
    aws_access_key_id='s4admin', aws_secret_access_key='s4secret', region_name='us-east-1')
client.put_bucket_policy(Bucket='troshka-images', Policy=json.dumps({
    'Version': '2012-10-17',
    'Statement': [{'Effect': 'Allow', 'Principal': {'AWS': ['arn:aws:iam:::user/troshka']},
        'Action': ['s3:*'], 'Resource': ['arn:aws:s3:::troshka-images', 'arn:aws:s3:::troshka-images/*']}]
}))
"
```

Then create the S3 provider in the UI with access key `troshka` and the generated secret key.

### Read-Only S3 (central gold images)

For shared disk images and patterns distributed to all instances. Created as an `s3_readonly` provider. The central S4 must have:

- **S3 keys matching the source** — library items must use the same `library/{library_id}/{item_id}/{name}.{format}` path structure as the original AWS S3 bucket. Pattern disks under `patterns/{pattern_id}/` are already correct if copied with `rclone`.
- **External Route** — same requirement as read-write, target clusters must be able to reach it.
- **Custom user** — same `radosgw-admin` + bucket policy approach as local S4. Do NOT change the S4 env vars — this breaks existing data access.

The backend automatically detects whether each disk comes from local or central S3 based on the `LibraryItem.source` field and `head_object` probes against both buckets.

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
| Storage | Ceph-NFS mount inside host VM | Ceph RBD PVCs via CDI `source.s3` DataVolumes |
| Image download | troshkad boto3 from S3 | CDI `source.s3` with `secretRef` |
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
- **ClusterRoleBinding namespace mismatch** — the binding subjects must reference the namespace where the operator is deployed. The backend patches this automatically on provider creation.

### Pods failing with SCC errors

```bash
oc get events -n troshka-<project-id> --sort-by='.lastTimestamp'
```

Common causes:
- **`troshka-network-pods` SCC missing** — run Step 3
- **SA not in SCC users list** — the operator patches this automatically, but the SCC must exist first

### Deploy stuck at "images: waiting"

The operator downloads disk images from S3 via CDI DataVolumes into a `troshka-cache` namespace. Check:

```bash
# DataVolume status
oc get datavolumes -n troshka-cache

# Importer pod logs
oc get pods -n troshka-cache
oc logs <importer-pod> -n troshka-cache
```

Common causes:
- **S3 endpoint not reachable** — target cluster can't reach the S3 Route. Verify with `curl` from a pod on the target cluster.
- **S3 credentials incorrect** — check the `s3-credentials` and `s3-central-credentials` Secrets in `troshka-cache` namespace. CDI expects `accessKeyId` and `secretKey` keys.
- **Wrong S3 bucket** — if the importer shows `NoSuchKey`, the disk exists in the central bucket but the DataVolume was created with the local S3 config. Check that `centralS3Config` is set on the TroshkaProject CR and that the storage node's `centralSource` flag is `true` in the topology.
- **Stale cache** — golden PVCs from previous failed deploys may be reused. Delete all DataVolumes and PVCs in `troshka-cache` and redeploy.
- **CDI not installed** — verify `oc get cdi` returns a healthy CDI instance.

### Namespace stuck in Terminating after project delete

The operator's finalizers on TroshkaProject/TroshkaNetwork CRs can block namespace deletion. Force-clean:

```bash
# Remove finalizers from CRs
for cr in $(oc get troshkaproject,troshkanetwork -n troshka-<id> -o name); do
  oc patch "$cr" -n troshka-<id> --type=merge -p '{"metadata":{"finalizers":[]}}'
done
```
