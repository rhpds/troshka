# Troshka Installation Guide: OpenShift Virtualization

This guide covers setting up Troshka on OpenShift Virtualization (KubeVirt). For backend, frontend, and database setup, see [Common Setup](install-common.md).

## Prerequisites

- OpenShift 4.x cluster with OpenShift Virtualization operator installed
- Worker nodes with nested virtualization capability
  - AMD EPYC recommended (better nested virt performance than Intel)
  - Check capability: `oc debug node/<node-name> -- chroot /host/ cat /proc/cpuinfo | grep -E 'vmx|svm'`
- Ceph storage with NFS capability (`ocs-storagecluster-ceph-rbd-virtualization` storage class available)
- `oc` CLI installed and logged in as cluster admin
- Troshka backend running (see [Common Setup](install-common.md))
- MetalLB or equivalent LoadBalancer service provider configured on the cluster

## RBAC Setup

Apply the RBAC manifest to create the service account and permissions:

```bash
oc apply -f infra/ocpvirt-rbac.yaml
```

This manifest creates:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: troshka
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: troshka
  namespace: troshka
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: troshka-provider
rules:
  # KubeVirt VMs — full lifecycle
  - apiGroups: ["kubevirt.io"]
    resources: ["virtualmachines"]
    verbs: ["create", "delete", "get", "list", "patch"]
  # KubeVirt VMIs — read-only for status checks
  - apiGroups: ["kubevirt.io"]
    resources: ["virtualmachineinstances"]
    verbs: ["get", "list"]
  # CDI DataVolumes — created via VM dataVolumeTemplates
  - apiGroups: ["cdi.kubevirt.io"]
    resources: ["datavolumes"]
    verbs: ["create", "delete", "get", "list"]
  # Services — NodePort for SSH/agent, ClusterIP for vncd
  - apiGroups: [""]
    resources: ["services"]
    verbs: ["create", "delete", "get", "list"]
  # PVCs — Ceph-NFS storage pools, extend
  - apiGroups: [""]
    resources: ["persistentvolumeclaims"]
    verbs: ["create", "delete", "get", "list", "patch"]
  # PVs — read NFS endpoint from bound PV
  - apiGroups: [""]
    resources: ["persistentvolumes"]
    verbs: ["get", "list"]
  # Routes — console edge termination
  - apiGroups: ["route.openshift.io"]
    resources: ["routes"]
    verbs: ["create", "delete", "get", "list"]
  # Secrets — cloud-init userdata
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["create", "delete", "get"]
  # Namespace — ensure troshka namespace exists
  - apiGroups: [""]
    resources: ["namespaces"]
    verbs: ["get", "create"]
  # Nodes — find IPs for NodePort access
  - apiGroups: [""]
    resources: ["nodes"]
    verbs: ["get", "list"]
  # Pods — find Rook toolbox for ceph commands
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list"]
  # Pod exec — run ceph CLI in Rook toolbox (NFS export management)
  - apiGroups: [""]
    resources: ["pods/exec"]
    verbs: ["create", "get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: troshka-provider
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: troshka-provider
subjects:
  - kind: ServiceAccount
    name: troshka
    namespace: troshka
```

Verify RBAC:

```bash
oc auth can-i create virtualmachines --as=system:serviceaccount:troshka:troshka
# Should return: yes
```

## Generate Token

Create a 1-year service account token:

```bash
oc create token troshka -n troshka --duration=8760h
```

Save this token securely — you will need it for the provider credentials.

## Create Provider

1. Navigate to Admin → Providers
2. Click "Add Provider"
3. Fill in the form:
   - **Name**: `ocpvirt-prod`
   - **Type**: Select "OpenShift Virtualization"
   - **API URL**: `https://api.cluster.example.com:6443`
   - **Token**: Paste the token from the previous step
   - **Namespace**: `troshka`
   - **Verify SSL**: Uncheck (for self-signed certs)
4. Click "Create Provider"
5. Click "Test" to verify connectivity

<details>
<summary>API equivalent</summary>

```bash
export TROSHKA_TOKEN="<your-troshka-api-token>"
export OCP_API_URL="https://api.cluster.example.com:6443"
export OCP_TOKEN="<token-from-previous-step>"

curl -X POST http://localhost:8200/api/v1/providers \
  -H "Authorization: Bearer $TROSHKA_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"name\": \"ocpvirt-prod\",
    \"type\": \"ocpvirt\",
    \"credentials\": {
      \"api_url\": \"$OCP_API_URL\",
      \"token\": \"$OCP_TOKEN\",
      \"namespace\": \"troshka\",
      \"verify_ssl\": false
    }
  }"
```

Test connectivity:

```bash
curl -X POST "http://localhost:8200/api/v1/providers/<provider-id>/test" \
  -H "Authorization: Bearer $TROSHKA_TOKEN"
```

</details>

## Infrastructure Setup

1. Navigate to Admin → Providers
2. Click "Setup Infrastructure" on your OCP Virt provider
3. Wait for the verification to complete

This verifies:
- API connectivity and token validity
- Namespace exists and is accessible
- Storage classes are available
- Worker nodes have nested virtualization capability

<details>
<summary>API equivalent</summary>

```bash
curl -X POST "http://localhost:8200/api/v1/providers/<provider-id>/setup-infra" \
  -H "Authorization: Bearer $TROSHKA_TOKEN"
```

</details>

## Storage

OCP Virt hosts use Ceph storage via the `ocs-storagecluster-ceph-rbd-virtualization` storage class. Two PVCs are created automatically during host provisioning:

- Root disk: 50 GiB (OS and system files)
- Data disk: user-specified size (VM images and workloads)

Storage is shared by design (Ceph RBD) — hosts in the same storage pool can live-migrate VMs between each other. See the Storage Pools section in [Common Setup](install-common.md) for migration configuration.

## Host Image

OCP Virt hosts require a RHEL 9 base image. The driver supports two methods:

### Method 1: DataSource (Recommended)

Use a pre-imported DataSource from the `openshift-virtualization-os-images` namespace.

First, list available DataSources:

```bash
oc get datasources -n openshift-virtualization-os-images
```

Then configure the image via UI:

1. Navigate to Admin → Providers
2. Select your OCP Virt provider
3. In the Image Settings section, enter the DataSource name (e.g., `rhel9`)
4. Click "Save"

<details>
<summary>API equivalent</summary>

```bash
curl -X POST "http://localhost:8200/api/v1/providers/<provider-id>/set-image" \
  -H "Authorization: Bearer $TROSHKA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"image_id": "rhel9"}'
```

</details>

### Method 2: HTTP URL

1. Navigate to Admin → Providers
2. Select your OCP Virt provider
3. In the Image Settings section, enter the RHEL 9 QCOW2 image URL
4. Click "Save"

<details>
<summary>API equivalent</summary>

```bash
curl -X POST "http://localhost:8200/api/v1/providers/<provider-id>/set-image" \
  -H "Authorization: Bearer $TROSHKA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"rhel_image_url": "https://example.com/rhel-9.5-x86_64-kvm.qcow2"}'
```

</details>

## RHEL Installation ISO

OCP Virt hosts need a RHEL installation ISO for cloud-init package installation. Create a PVC with the ISO:

```bash
# Download RHEL ISO
curl -o rhel-10.2-dvd.iso 'https://access.redhat.com/downloads/...'

# Create PVC and upload
oc create -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: rhel-10.2-dvd-iso
  namespace: troshka
spec:
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 10Gi
  storageClassName: ocs-storagecluster-ceph-rbd-virtualization
EOF

# Upload ISO to PVC (use virtctl or direct upload)
virtctl image-upload pvc rhel-10.2-dvd-iso \
  --image-path=rhel-10.2-dvd.iso \
  --namespace=troshka \
  --insecure
```

The ISO PVC name must match the `iso_pvc` credential field (default: `rhel-10.2-dvd-iso`). If using a different name:

1. Navigate to Admin → Providers
2. Select your OCP Virt provider
3. Update the ISO PVC name in the credentials section
4. Click "Save"

<details>
<summary>API equivalent</summary>

```bash
curl -X PATCH "http://localhost:8200/api/v1/providers/<provider-id>" \
  -H "Authorization: Bearer $TROSHKA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"credentials": {"iso_pvc": "your-iso-pvc-name"}}'
```

</details>

## Console Setup

OCP Virt uses OpenShift Routes for console access (edge TLS termination by the OCP router). No external DNS or Let's Encrypt certificates are needed.

1. Navigate to Admin → Providers
2. Click "Setup Console" on your OCP Virt provider
3. Enter the base domain (typically the cluster's wildcard apps domain, e.g., `apps.cluster.example.com`)
4. Click "Setup"

This configures the console domain. When hosts are provisioned, the driver creates:
- A Service for `troshka-vncd` (ClusterIP on port 8080)
- A Route with edge TLS termination (auto-generated hostname: `troshka-console-{host-id}-troshka.apps.cluster.example.com`)

The `troshka-vncd` daemon runs without TLS (`--no-tls` flag) — all TLS is handled by the OCP router.

<details>
<summary>API equivalent</summary>

```bash
curl -X POST "http://localhost:8200/api/v1/providers/<provider-id>/setup-console" \
  -H "Authorization: Bearer $TROSHKA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"base_domain": "apps.cluster.example.com"}'
```

</details>

## Provision First Host

1. Navigate to Admin → Providers
2. Select your OCP Virt provider
3. Click "Add Host"
4. Fill in the form:
   - **Instance Type**: Enter in format `{vcpus}c-{ram_gb}g` (e.g., `64c-256g` for 64 vCPUs and 256 GiB RAM)
   - **Storage Size**: Enter data disk size in GB (e.g., `500`)
5. Click "Create Host"
6. Monitor the provisioning progress in the Hosts section

Instance type format: `{vcpus}c-{ram_gb}g`
- Example: `64c-256g` = 64 vCPUs, 256 GiB RAM
- Example: `32c-128g` = 32 vCPUs, 128 GiB RAM

The provisioner creates:
- VirtualMachine with 2 DataVolumes (root + data disks)
- LoadBalancer Service for SSH (port 22000) and agent (port 31337)
- Secret with cloud-init userdata

Host provisioning typically takes 5-10 minutes (VM boot + package installation via ISO repos).

<details>
<summary>API equivalent</summary>

```bash
curl -X POST http://localhost:8200/api/v1/hosts \
  -H "Authorization: Bearer $TROSHKA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "provider_id": "<provider-id>",
    "instance_type": "64c-256g",
    "storage_size_gb": 500
  }'
```

</details>

## Differences from Cloud Providers

| Feature | Cloud (AWS/GCP/Azure) | OCP Virt |
|---------|----------------------|----------|
| External IPs | Yes — Elastic IPs, static IPs | Via MetalLB LoadBalancer |
| `externalAccess` toggle | Functional | Disabled (not supported) |
| Resize | Yes (stop/resize/start) | No — KubeVirt limitation |
| Console TLS | certbot + DNS provider | OCP Routes (edge TLS) |
| Shared storage | FSx / Azure Files / BYO NFS | Ceph-NFS (built-in) |
| Host access | SSH directly via public IP | LoadBalancer service |
| S3 storage | AWS S3 / compatible | AWS S3 (external) |
| Network setup | VPC/VNet creation required | OCP networking (built-in) |
| SSH user | `ec2-user` / `troshka` | `cloud-user` |
| Data disk | `/dev/nvme1n1` or `/dev/sdb` | `/dev/vdb` |

## Verify

Checklist:

- [ ] Provider test passes
- [ ] RBAC verified: `oc auth can-i create virtualmachines --as=system:serviceaccount:troshka:troshka` returns "yes"
- [ ] Host VM running: `oc get vm -n troshka` shows VM in "Running" phase
- [ ] Agent connected: `GET /api/v1/hosts` shows host status "connected"
- [ ] Console working: browse to the Route URL and see noVNC interface
- [ ] Deploy test: create a project, deploy a VM topology successfully

Check VM status:

```bash
oc get vm -n troshka
oc get vmi -n troshka
```

Check LoadBalancer service:

```bash
oc get svc -n troshka | grep troshka-lb
```

Check console Route:

```bash
oc get route -n troshka | grep troshka-console
```

Access the host via LoadBalancer SSH:

```bash
# Get LoadBalancer external IP
LB_IP=$(oc get svc troshka-lb-<host-id-short> -n troshka -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

# SSH to host (use private key from Host.private_key in database)
ssh -i /path/to/host-key -p 22000 cloud-user@$LB_IP
```

## Troubleshooting

### VM stuck in Pending

Check VMI events:

```bash
oc describe vmi troshka-host-<id> -n troshka
```

Common causes:
- DataVolume not bound (check PVC status)
- Node scheduling issues (check node resources)
- Image import failed (check CDI importer pod logs)

### No external IP assigned

Check LoadBalancer configuration:

```bash
oc get svc troshka-lb-<id> -n troshka -o yaml
```

If MetalLB is not configured, the LoadBalancer will stay in "Pending" state. Install MetalLB or switch to NodePort services.

### Console not accessible

Check Route creation:

```bash
oc get route troshka-console-<id> -n troshka -o yaml
```

Verify vncd Service is running:

```bash
oc get svc troshka-vncd-<id> -n troshka
```

Check vncd daemon logs in the VM (SSH to host first):

```bash
sudo journalctl -u troshka-vncd -f
```

### Agent not connecting

Check troshkad daemon status on the host:

```bash
ssh -i /path/to/key -p 22000 cloud-user@<lb-ip>
sudo systemctl status troshkad
sudo journalctl -u troshkad -f
```

Common causes:
- Packages failed to install from ISO repos (check cloud-init logs: `sudo cat /var/log/cloud-init.log`)
- Agent token mismatch (check `/etc/troshka-agent/config.yaml`)
- Firewall blocking port 31337 (should not happen — OCP networking handles this)

## Next Steps

- [Create a storage pool](install-common.md#storage-pools) for live migration
- [Set up DNS provider](install-common.md#dns-providers) for automated DNS records
- [Import library items](install-common.md#library) for VM deployment
- [Deploy your first project](../README.md#quick-start)
