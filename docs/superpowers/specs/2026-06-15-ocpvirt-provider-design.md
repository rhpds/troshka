# OCP Virt Provider for Troshka

**Date:** 2026-06-15
**Status:** Draft
**Author:** prutledg + Claude

## Overview

Add OpenShift Virtualization (OCP Virt / KubeVirt) as a second provider type alongside AWS EC2. A Troshka "host" on OCP Virt is a large RHEL VM with nested virtualization enabled, running the same troshkad agent. Everything inside the VM — libvirt, VXLAN, nftables, network namespaces, deploy pipeline, pattern capture — is unchanged.

### Core Insight

The nested-virt approach means OCP Virt is a provisioning concern, not an architecture change. Troshkad doesn't know or care that it's inside a KubeVirt VM. The entire Troshka stack above the host provisioning layer is provider-agnostic.

## Reference Cluster

Design validated against `ocpvdev01.dal13.infra.demo.redhat.com`:
- **Workers:** 4x AMD EPYC 7763 bare metal (256 vCPU, 1 TB RAM each), nested virt enabled (`kvm_amd nested=1`)
- **Storage:** Ceph ODF — RBD (default, 2x replication), CephFS (3x replication), NFS Ganesha
- **Capacity:** 28 TiB raw, ~13 TiB free, ~2.7 TiB usable on CephFS/NFS
- **CPU model:** `host-passthrough` (nested VMs see real AMD EPYC)
- **Networking:** OVN with bridge and layer2 NADs, 3 router replicas

## Architecture

### Provider Abstraction

Extract provider-specific operations from the current monolithic `provisioner.py` into pluggable modules:

```
src/backend/app/services/providers/
├── __init__.py      # get_provider_driver(provider) dispatcher
├── base.py          # Abstract interface
├── ec2.py           # Current AWS code, extracted unchanged
└── ocpvirt.py       # New OCP Virt implementation
```

**Provider interface (`base.py`):**

```python
class ProviderDriver:
    def provision_host(self, provider, host_id, instance_type, storage_size_gb, **kwargs) -> dict:
        """Returns dict with host_id, instance_id, public_ip, private_ip,
        total_vcpus, total_ram_mb, private_key, key_pair_name, etc."""

    def terminate_host(self, provider, instance_id) -> None: ...

    def resize_host(self, provider, instance_id, new_instance_type) -> dict: ...

    def setup_console(self, provider, base_domain) -> dict:
        """Returns console config (zone_id, base_domain, nameservers, etc.)"""

    def create_console_record(self, provider, hostname, ip_address) -> None: ...

    def delete_console_record(self, provider, hostname) -> None: ...

    def extend_host_storage(self, provider, host, increment_gb) -> None: ...

    def get_provider_status(self, provider) -> dict: ...
```

**Dispatcher (`__init__.py`):**

```python
def get_provider_driver(provider) -> ProviderDriver:
    if provider.type == "ec2":
        return EC2Driver()
    elif provider.type == "ocpvirt":
        return OCPVirtDriver()
    raise ValueError(f"Unknown provider type: {provider.type}")
```

### What Changes vs. What Stays

| Layer | Changes? | Details |
|-------|----------|---------|
| Provider model | Yes | New `type="ocpvirt"`, credentials store k8s token + API URL |
| Provisioner | Yes | Extracted into provider modules |
| Agent deployer | No | Same SSH + cloud-init install script |
| troshkad | No | Identical daemon |
| troshka-vncd | No | Identical daemon |
| Networking (VXLAN/nftables/netns) | No | All inside host VM |
| Deploy pipeline | No | Same parallel VM creation |
| Pattern capture (NBD) | No | Same qemu-img + S3 upload |
| Health poller | Minor | Abstract auto-extend (EBS → provider-specific) |
| Storage pool service | Yes | New `shared-ceph-nfs` mode alongside `shared-fsx` and `shared-byo` |
| Console DNS | Yes | OCP Routes instead of Route53 A records |
| Frontend | Minor | New provider type in admin UI, instance type field changes |
| Canvas / topology / projects | No | Unchanged |
| Library / patterns / snapshots | No | Unchanged |

## Detailed Design

### 1. Provider Model Changes

Add new provider type `ocpvirt`. The existing `credentials` JSON field stores:

```json
{
  "api_url": "https://api.ocpvdev01.dal13.infra.demo.redhat.com:6443",
  "token": "sha256~...",
  "namespace": "troshka",
  "verify_ssl": false
}
```

Existing AWS-specific columns (`vpc_id`, `subnet_id`, `security_group_id`, `default_ami`, `default_region`) remain nullable and are simply unused for OCP Virt providers. No schema changes needed on the Provider model.

**Provider creation API** (`POST /providers`): The `ProviderCreate` schema needs to accept `type` as `"ec2"` or `"ocpvirt"`. For OCP Virt, the relevant fields are:
- `name`, `type="ocpvirt"`
- `api_url`, `token`, `namespace` (stored in credentials JSON)
- `verify_ssl` (default false for self-signed OCP certs)

### 2. Host Provisioning (OCP Virt)

**`OCPVirtDriver.provision_host()`** creates a KubeVirt VirtualMachine CR:

```yaml
apiVersion: kubevirt.io/v1
kind: VirtualMachine
metadata:
  name: troshka-host-{host_id[:8]}
  namespace: troshka
  labels:
    app: troshka
    troshka/host-id: {host_id}
    troshka/host-type: {shared|pattern_buffer}
spec:
  running: true
  dataVolumeTemplates:
  - metadata:
      name: troshka-host-{host_id[:8]}-root
    spec:
      source:
        http:
          url: {rhel9_qcow2_url}  # or sourceRef to a DataSource
      storage:
        resources:
          requests:
            storage: {storage_size_gb}Gi
        storageClassName: ocs-storagecluster-ceph-rbd-virtualization
  template:
    spec:
      domain:
        cpu:
          cores: {cores}
          sockets: {sockets}
          threads: 1
          model: host-passthrough
        memory:
          guest: {memory_gi}Gi
        devices:
          disks:
          - disk:
              bus: virtio
            name: rootdisk
          - disk:
              bus: virtio
            name: cloudinitdisk
          interfaces:
          - masquerade: {}
            model: virtio
            name: default
        features:
          kvm:
            hidden: false   # expose KVM to guest for nested virt
      networks:
      - name: default
        pod: {}
      volumes:
      - dataVolume:
          name: troshka-host-{host_id[:8]}-root
        name: rootdisk
      - cloudInitNoCloud:
          userData: |
            #cloud-config
            user: ec2-user
            ssh_authorized_keys:
              - {generated_ssh_pubkey}
            packages:
              - qemu-kvm
              - libvirt
              - nfs-utils
            runcmd:
              - systemctl enable --now libvirtd
        name: cloudinitdisk
```

**Instance type mapping:** Admin specifies CPU and memory when adding a host (or picks from presets like "64 vCPU / 256 GB", "128 vCPU / 512 GB"). The `instance_type` field on the Host model stores a descriptor string like `"64c-256g"`. The driver translates this to `cores`/`sockets`/`memory` in the VM spec.

**Provisioning flow:**
1. Generate SSH keypair
2. Create VirtualMachine CR via kubernetes client
3. Wait for VMI (VirtualMachineInstance) to reach `Running` phase
4. Get pod IP from VMI status (this is the host's management IP)
5. SSH in and run agent deployer (same `deploy_agent()` function)
6. Host connects back to Troshka API via the pod network

**SSH access for agent install:** The VM gets the generated SSH public key via cloud-init. The backend SSHes to the VM's pod IP to run the agent install script (same `deploy_agent()` function). Since Troshka's backend typically runs outside the cluster, the VM's pod IP is not directly reachable. Solution: create a temporary NodePort Service for SSH during provisioning:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: troshka-ssh-{host_id[:8]}
  namespace: troshka
spec:
  type: NodePort
  selector:
    kubevirt.io/domain: troshka-host-{host_id[:8]}
  ports:
  - port: 22
    targetPort: 22
```

The provisioning flow reads back the assigned NodePort and SSHes to `{any_node_ip}:{nodeport}`. After agent installation completes and troshkad connects back, the NodePort Service is deleted. This is a one-time setup path — all further communication uses the troshkad HTTPS API on port 31337 (also exposed via a persistent Service).

**Troshkad Service (persistent):**
```yaml
apiVersion: v1
kind: Service
metadata:
  name: troshka-agent-{host_id[:8]}
  namespace: troshka
spec:
  type: NodePort
  selector:
    kubevirt.io/domain: troshka-host-{host_id[:8]}
  ports:
  - port: 31337
    targetPort: 31337
```

The troshkad NodePort is how the backend reaches the agent after provisioning. The `ip_address` field on the Host model stores `{node_ip}:{nodeport}` (or just the node IP, with the port tracked separately). The health poller and all troshkad client calls use this endpoint.

### 3. Host Termination

**`OCPVirtDriver.terminate_host()`:**
1. Delete the VirtualMachine CR (cascades to VMI, DataVolumes, PVCs)
2. Delete any associated Services and Routes
3. Clean up NFS subvolume if this was the last host in a pool

### 4. Storage: Shared Ceph-NFS

**New storage pool mode:** `shared-ceph-nfs` (alongside existing `shared-fsx`, `shared-byo`, `local`).

**Pool creation flow:**
1. Admin creates a storage pool with `mode="shared-ceph-nfs"` on an OCP Virt provider
2. Backend creates a PVC with `ReadWriteMany` access mode using the `ocs-storagecluster-ceph-nfs` storage class. This triggers the NFS CSI provisioner to automatically create a CephFS subvolume and NFS Ganesha export:
   ```yaml
   apiVersion: v1
   kind: PersistentVolumeClaim
   metadata:
     name: troshka-pool-{pool_id[:8]}
     namespace: troshka
   spec:
     accessModes:
       - ReadWriteMany
     resources:
       requests:
         storage: {quota_gb}Gi
     storageClassName: ocs-storagecluster-ceph-nfs
   ```
3. Backend reads the resulting PV to extract the NFS server address and export path
4. The NFS endpoint is stored on the StoragePool model (reusing existing `nfs_endpoint` field)

This approach uses the standard k8s PVC flow — no need to exec into Ceph tools pods or call the Ceph CLI directly. The NFS CSI driver handles subvolume creation, export setup, and quota enforcement.

**How it works at runtime:**
- When a host VM is provisioned into this pool, `deploy_agent()` receives `storage_mode="shared"`, `nfs_server`, `nfs_path` — identical to `shared-byo`
- The agent install script mounts NFS at `/var/lib/troshka/shared/`
- All shared storage code paths (image cache, pattern cache, VM disks) work unchanged

**Ceph NFS access from VMs:**
- Host VMs use the pod network (masquerade) to reach the NFS Ganesha service
- Cloud-init configures `/etc/resolv.conf` to use cluster DNS (`172.30.0.10`) so the VM can resolve `rook-ceph-nfs-ocs-storagecluster-cephnfs-a.openshift-storage.svc.cluster.local`
- Standard NFS mount with `nfs-utils` (installed via cloud-init)

**Storage pool model changes:** Add `ceph_subvolume_group` field (nullable string) to StoragePool for tracking the created CephFS subvolume group name.

### 5. Pattern Buffer

**Same architecture as AWS:** A dedicated VM in the `troshka` namespace with `host_type="pattern_buffer"`.

**Differences from regular host VMs:**
- No libvirt installation (same as AWS pattern buffer)
- Gets a large RBD volume for scratch space (instead of NVMe instance storage)
- The scratch volume is a second DataVolume on the VM spec, mounted at `/var/lib/troshka/local`
- Runs qemu-img, NBD capture, S3 upload — all unchanged

**Scratch volume spec:**
```yaml
- metadata:
    name: troshka-pb-{host_id[:8]}-scratch
  spec:
    source:
      blank: {}
    storage:
      resources:
        requests:
          storage: 500Gi
      storageClassName: ocs-storagecluster-ceph-rbd-virtualization
```

**Pattern buffer provisioning** is triggered the same way — `pattern_buffer_service.py` calls `provision_host()` with `host_type="pattern_buffer"`, which dispatches to `OCPVirtDriver`. The driver creates the VM with the extra scratch volume and skips libvirt in the cloud-init.

### 6. Console (VNC) Access

**Replace Route53 DNS + certbot with OCP Routes using edge TLS termination.**

The OCP router terminates TLS using the wildcard `*.apps.{cluster_domain}` cert and forwards plain traffic to vncd. This eliminates certbot and Let's Encrypt entirely for OCP Virt hosts.

**vncd change:** Add a `--no-tls` flag to `troshka-vncd`. When set, vncd listens for plain WebSocket connections on port 8080 instead of TLS on 443. The cloud-init / agent install script passes this flag when the host is on an OCP Virt provider.

**Per-host console Route:**
```yaml
apiVersion: route.openshift.io/v1
kind: Route
metadata:
  name: troshka-console-{host_id[:8]}
  namespace: troshka
spec:
  host: {host_id[:8]}.apps.{cluster_domain}
  to:
    kind: Service
    name: troshka-vncd-{host_id[:8]}
  port:
    targetPort: 8080
  tls:
    termination: edge
    insecureEdgeTerminationPolicy: Redirect
```

**Per-host vncd Service:**
```yaml
apiVersion: v1
kind: Service
metadata:
  name: troshka-vncd-{host_id[:8]}
  namespace: troshka
spec:
  selector:
    kubevirt.io/domain: troshka-host-{host_id[:8]}
  ports:
  - port: 8080
    targetPort: 8080
```

**Console setup (`setup_console` on OCP Virt provider):**
- No Route53 hosted zone needed
- No IAM role/instance profile needed
- No certbot needed (OCP wildcard cert handles TLS at the router)
- `console_base_domain` = `apps.{cluster_domain}` (auto-detected from API URL or configured)
- Each host's `console_domain` = `{host_id[:8]}.apps.{cluster_domain}`

**Route and Service lifecycle:** Created when a host is provisioned, deleted when terminated. The `create_console_record()` driver method creates the Route + Service; `delete_console_record()` removes them.

### 7. Health Poller Changes

**Auto-extend abstraction:** Extract `extend_host_ebs()` calls into the provider driver interface:
- `EC2Driver.extend_host_storage()` — current EBS ModifyVolume logic
- `OCPVirtDriver.extend_host_storage()` — Ceph RBD resize via k8s PVC patch (increase `spec.resources.requests.storage`)

The health poller calls `get_provider_driver(host.provider).extend_host_storage()` instead of directly calling `extend_host_ebs()`.

**Everything else in the health poller is unchanged** — partition threshold evaluation, storage warnings, agent health checks, cert re-signing all work identically.

### 8. Frontend Changes

**Provider admin page:**
- "Add Provider" dialog: new type option "OCP Virt" alongside "EC2"
- OCP Virt provider fields: Name, API URL, Token, Namespace, Verify SSL
- EC2 provider fields: unchanged

**Host admin page:**
- "Add Host" for OCP Virt provider: CPU (cores) and Memory (GB) fields instead of EC2 instance type dropdown
- Optional: preset sizes ("64 vCPU / 256 GB", "128 vCPU / 512 GB") as convenience buttons
- Host cards show provider type badge

**Storage pool page:**
- New pool mode "Shared Ceph-NFS" in dropdown
- Fields: Storage quota (GB)
- No FSx-specific fields (throughput, AZ)

**No changes to:** Canvas, topology editor, deploy dialog, patterns page, library page, projects page, console viewer.

### 9. Kubernetes Client

**New dependency:** `kubernetes` Python package (official k8s client library).

**Client initialization:**
```python
from kubernetes import client, config

def _get_k8s_client(credentials: dict):
    configuration = client.Configuration()
    configuration.host = credentials["api_url"]
    configuration.api_key = {"authorization": f"Bearer {credentials['token']}"}
    configuration.verify_ssl = credentials.get("verify_ssl", False)
    api_client = client.ApiClient(configuration)
    return api_client
```

**APIs used:**
- `CustomObjectsApi` — for VirtualMachine CRDs (`kubevirt.io/v1`)
- `CoreV1Api` — for Services, ConfigMaps
- `CustomObjectsApi` — for Routes (`route.openshift.io/v1`)
- `CoreV1Api` — for PVC resize (auto-extend)

### 10. External Access (EIPs)

**Not applicable for OCP Virt.** On AWS, EIPs provide public IPs for nested VMs via secondary ENI addresses + nftables DNAT. On OCP Virt, nested VMs are not publicly accessible — they're behind the pod network.

If external access to nested VMs is needed in the future, it could be done via:
- NodePort Services per nested VM port
- OCP Routes for HTTP/HTTPS workloads
- MetalLB LoadBalancer services

For now, this is explicitly out of scope. The `externalAccess` toggle on gateway nodes in the topology would be disabled/hidden for OCP Virt projects.

### 11. Namespace Setup

On first use of an OCP Virt provider, the backend ensures the target namespace exists:

```python
def _ensure_namespace(api_client, namespace):
    v1 = client.CoreV1Api(api_client)
    try:
        v1.read_namespace(namespace)
    except ApiException as e:
        if e.status == 404:
            v1.create_namespace(client.V1Namespace(
                metadata=client.V1ObjectMeta(name=namespace)
            ))
        else:
            raise
```

The namespace is created once and reused for all VMs, Services, Routes, and PVCs belonging to that provider.

### 12. RHEL Base Image

Troshka host VMs need a RHEL 9 base image. Options:
1. **Pre-uploaded DataSource** in `openshift-virtualization-os-images` namespace (like the existing Fedora/RHEL templates)
2. **HTTP URL** in the DataVolume `source.http.url` pointing to a RHEL 9 qcow2
3. **Registry source** pulling from a container registry

**Recommended:** Use `sourceRef` pointing to an existing RHEL 9 DataSource if available on the cluster, or fall back to HTTP URL. The provider credentials could include an optional `rhel_image_url` field, or the backend auto-detects available DataSources.

## Migration Path

### Phase 1: Provider Abstraction (refactor only, no new features)
- Extract `provisioner.py` into `providers/ec2.py`
- Create `providers/base.py` interface
- Create `providers/__init__.py` dispatcher
- Update all callers to use `get_provider_driver()`
- All existing tests must pass — pure refactor

### Phase 2: OCP Virt Provider (new feature)
- Implement `providers/ocpvirt.py`
- Add `kubernetes` dependency
- Storage pool: `shared-ceph-nfs` mode
- Console: OCP Route-based
- Frontend: provider type selector, OCP Virt fields
- Pattern buffer on OCP Virt

### Phase 3: Testing & Hardening
- End-to-end test on `ocpvdev01` cluster
- Failure modes: VM scheduling failures, Ceph capacity, NFS connectivity
- Health poller integration with RBD resize
- Console latency validation through OCP router

## Open Questions

1. **RHEL image source:** Pre-upload a DataSource to the cluster, or specify an HTTP URL per provider? DataSource is cleaner but requires one-time cluster setup. Recommendation: support both — check for DataSource first, fall back to HTTP URL from provider config.
2. **Resource quotas:** Should Troshka set ResourceQuotas on its namespace to prevent runaway resource consumption? Recommendation: defer to Phase 3 — not needed for initial implementation.
3. **Resize support:** Resizing a KubeVirt VM requires stop → modify spec → start (no hot-resize for CPU/memory in KubeVirt). Recommendation: disable resize for OCP Virt hosts initially — add in a future iteration if needed.
