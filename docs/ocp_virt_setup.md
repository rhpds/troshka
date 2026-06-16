# OCP Virt Provider Setup

## Prerequisites

- OpenShift cluster with OpenShift Virtualization (CNV) installed
- Ceph ODF storage with NFS Ganesha enabled (`ocs-storagecluster-ceph-nfs` storage class)
- Nested virtualization enabled on worker nodes (`kvm_amd` or `kvm_intel` nested=1)
- `host-passthrough` CPU model configured in HyperConverged CR

## Service Account Setup

Apply the RBAC manifest to create the service account, ClusterRole, and binding:

```bash
oc apply -f infra/ocpvirt-rbac.yaml
```

This creates:
- **Namespace**: `troshka`
- **ServiceAccount**: `troshka` in `troshka` namespace
- **ClusterRole**: `troshka-provider` with least-privilege permissions
- **ClusterRoleBinding**: binds the role to the SA

### Permissions (least-privilege)

| API Group | Resource | Verbs | Purpose |
|-----------|----------|-------|---------|
| `kubevirt.io` | `virtualmachines` | create, delete, get, list, patch | VM lifecycle + start/stop |
| `kubevirt.io` | `virtualmachineinstances` | get, list | Status checks, pod IP |
| `cdi.kubevirt.io` | `datavolumes` | create, delete, get, list | VM disk provisioning |
| (core) | `services` | create, delete, get, list | NodePort (SSH, agent), ClusterIP (vncd) |
| (core) | `persistentvolumeclaims` | create, delete, get, list, patch | Ceph-NFS pools, storage extend |
| (core) | `persistentvolumes` | get, list | Read NFS endpoint from bound PV |
| `route.openshift.io` | `routes` | create, delete, get, list | Console edge Routes |
| (core) | `namespaces` | get, create | Ensure namespace exists |
| (core) | `nodes` | get, list | Find node IPs for NodePort access |

### Generate Token

```bash
oc create token troshka -n troshka --duration=8760h
```

This generates a 1-year bearer token. Store it securely — it's the credential used by Troshka to manage VMs on the cluster.

## Create Provider in Troshka

1. Go to **Admin → Providers → + Add Provider**
2. Select type **OCP Virt**
3. Fill in:
   - **Name**: descriptive name (e.g., "OCP Virt Dev (dal13)")
   - **API URL**: `https://api.<cluster>.<domain>:6443`
   - **Token**: the SA token from above
   - **Namespace**: `troshka` (default)
   - **Verify SSL**: unchecked for self-signed certs
4. Click **Create**

### Console Setup

1. On the provider card, click **Setup Console**
2. Enter the apps domain: `apps.<cluster>.<domain>`
   - This is auto-derived from the API URL (replace `api.` with `apps.`)
   - Console uses OCP edge Routes — no Route53/certbot needed

## Provision a Host

1. Go to **Admin → Hosts → + Add Host**
2. Select the OCP Virt provider
3. Set **CPU Cores** and **Memory GB** (or use presets: 8c/32G, 64c/256G, 128c/512G)
4. Optionally select a **Storage Pool**
5. Click **Provision Host**

The backend will:
1. Create a VirtualMachine CR with nested virt enabled (`host-passthrough` CPU, KVM exposed)
2. Create NodePort Services for SSH (temporary) and troshkad (persistent)
3. Wait for VMI to reach Running
4. SSH in via NodePort and install troshkad (same agent as EC2)
5. Delete the SSH NodePort after agent connects
6. Create a console Route if console is configured

## Storage Pool Setup (Ceph-NFS)

1. Go to **Admin → Storage Pools → + Create Pool**
2. Select mode **Shared Ceph-NFS (OCP Virt)**
3. Set **Storage Quota** in GB
4. Select the OCP Virt provider
5. Click **Create**

This creates a `ReadWriteMany` PVC using the `ocs-storagecluster-ceph-nfs` storage class. The NFS endpoint is automatically extracted and mounted inside host VMs at `/var/lib/troshka/shared/`.

## Architecture

```
Browser → OCP Router (edge TLS) → vncd (plain WS :8080) → VNC

Troshka Backend → NodePort :agent_port → troshkad :31337 (HTTPS)

Host VM (KubeVirt):
  ├── troshkad (port 31337)
  ├── troshka-vncd --no-tls (port 8080)
  ├── libvirtd (nested VMs)
  ├── nftables / VXLAN / netns (same as EC2)
  └── NFS mount → Ceph-NFS → /var/lib/troshka/shared/
```

## Networking

Networking inside the host VM is **identical to EC2**:
- VXLAN overlays between hosts
- nftables for NAT and port forwarding
- Network namespaces for project isolation
- No EIPs (not applicable — nested VMs are behind pod network)

## Differences from EC2

| Feature | EC2 | OCP Virt |
|---------|-----|----------|
| Instance provisioning | EC2 RunInstances | KubeVirt VirtualMachine CR |
| Storage | EBS volumes | Ceph RBD PVCs |
| Shared storage | FSx OpenZFS | Ceph-NFS |
| Console TLS | Let's Encrypt (certbot) | OCP Router wildcard cert |
| External IPs (EIPs) | AWS Elastic IPs | Not supported |
| Host resize | EC2 ModifyInstance | Not supported |
| Auto-extend | EBS ModifyVolume | PVC patch |
| SSH for install | Public IP :22 | NodePort :random |

## Dev Cluster Reference

**ocpvdev01.dal13.infra.demo.redhat.com**:
- 4 worker nodes: AMD EPYC 7763 (256 vCPU, 1 TB RAM each)
- Ceph ODF: 28 TiB raw, ~2.7 TiB available on CephFS/NFS
- Nested virt: enabled (`kvm_amd nested=1`)
- CPU model: `host-passthrough`
