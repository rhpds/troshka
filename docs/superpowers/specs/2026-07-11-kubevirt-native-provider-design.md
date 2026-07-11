# KubeVirt Native Provider Design

**Date:** 2026-07-11
**Status:** Draft
**Author:** prutledg + Claude

## Overview

A new provider type (`kubevirt`) that creates KubeVirt VMs directly on an OCP cluster instead of provisioning host VMs with nested virtualization. All VM, networking, storage, BMC, PXE, and console operations are implemented as Kubernetes-native resources managed by a Troshka operator.

This is fully additive — zero changes to existing providers (ec2, ocpvirt, gcp, azure). Existing patterns are fully portable to the new provider.

## Goals

- KubeVirt VMs as first-class Troshka VMs (no nested virt, no troshkad)
- Full feature parity with existing providers except clock backdating
- Same patterns deployable across all provider types
- Kubernetes-native operator managing all resources
- Self-healing: crashed helper pods (gateway, dnsmasq, BMC) are restarted by the operator

## Non-Goals

- Clock backdating (KubeVirt doesn't expose `virsh domtime` equivalent)
- Replacing existing providers — this coexists alongside them
- Running without ODF/Ceph — Ceph RBD clone is required for performant pattern deploys

## Architecture

### Two New Components

1. **`kubevirt.py` provider driver** — thin layer in the backend that creates/watches CRDs instead of calling troshkad
2. **Troshka operator** — Kubernetes operator (Python/kopf) that reconciles CRDs into KubeVirt resources and helper pods

### CRD Schema

#### TroshkaProject (namespace-scoped)

The top-level resource. One per deployed Troshka project.

```yaml
apiVersion: troshka.redhat.com/v1alpha1
kind: TroshkaProject
metadata:
  name: project-{id[:8]}
  namespace: troshka-{project_id[:8]}  # one namespace per project for isolation
spec:
  projectId: "uuid"
  topology: { ... }  # Same JSONB topology blob as backend DB
  s3Config:
    bucket: "troshka-images"
    endpoint: "s3.amazonaws.com"
    region: "us-east-1"
    credentialsSecret: "s3-credentials"  # Secret ref
  registryCredentials:
    secretRef: "registry-creds"  # optional
  commonPassword: "..."  # optional, for pattern deploy credential override
status:
  phase: Pending | Deploying | Running | Stopping | Stopped | Destroying | Error
  vmStates:
    "vm-uuid-1": "Running"
    "vm-uuid-2": "Stopped"
  containerStates:
    "container-uuid-1": "Running"
  deployProgress:
    percent: 75
    stage: "Starting VMs"
    detail: "Starting bastion (2/5)"
  networks:
    - name: "project-net-1"
      ready: true
    - name: "bmc-net"
      ready: true
  message: ""  # error message if phase == Error
```

#### TroshkaNetwork (namespace-scoped, owned by TroshkaProject)

```yaml
apiVersion: troshka.redhat.com/v1alpha1
kind: TroshkaNetwork
metadata:
  name: net-{id[:8]}
  ownerReferences: [TroshkaProject]
spec:
  networkId: "uuid"
  cidr: "10.0.0.0/24"
  gateway: "10.0.0.1"
  dhcpRange: "10.0.0.100,10.0.0.200"
  networkType: standard | external | bmc
  staticLeases:
    - mac: "52:54:00:xx:xx:xx"
      ip: "10.0.0.10"
      hostname: "bastion"
  pxeConfig:  # optional
    enabled: true
    libraryIsoId: "uuid"
    isoS3Path: "library/uuid.iso"
  dnsForwarders: ["8.8.8.8"]
  externalAccess: true  # whether gateway provides outbound NAT
status:
  ready: true
  nadName: "net-{id[:8]}-nad"
  dhcpPodReady: true
  gatewayPodReady: true
  pxePodReady: true
```

#### TroshkaVM (namespace-scoped, owned by TroshkaProject)

```yaml
apiVersion: troshka.redhat.com/v1alpha1
kind: TroshkaVM
metadata:
  name: vm-{id[:8]}
  ownerReferences: [TroshkaProject]
spec:
  vmId: "uuid"
  name: "bastion"  # display name
  cpus: 4
  memory: 8192  # MB
  firmware: bios | uefi | uefi-secure
  machineType: q35 | i440fx
  smbiosUuid: "uuid"  # for BMC identification
  powerOnAtDeploy: true
  bootOrder:
    - type: disk
      id: "disk-uuid-1"
    - type: network
      id: "nic-uuid-1"
  disks:
    - id: "disk-uuid-1"
      sizeGb: 100
      bus: virtio | scsi | sata
      libraryImage:
        s3Path: "library/item-uuid.qcow2"
        format: qcow2
      patternImage:
        s3Path: "patterns/pattern-uuid/disk-uuid.qcow2"
        format: qcow2
    - id: "disk-uuid-2"
      sizeGb: 50
      bus: virtio
      blank: true  # empty disk, no image
  nics:
    - id: "nic-uuid-1"
      networkRef: "net-{id[:8]}"  # TroshkaNetwork name
      mac: "52:54:00:xx:xx:xx"
      model: virtio | e1000 | e1000e | igb | rtl8139
    - id: "nic-uuid-2"
      networkRef: "net-{id[:8]}-2"
      mac: "52:54:00:yy:yy:yy"
      model: virtio
  cloudInit:
    userData: |
      #cloud-config
      password: redhat
      chpasswd: { users: [{ name: cloud-user, password: redhat, type: text }] }
    networkConfig: |
      ...
  bmcEnabled: false
  cdrom:
    libraryIsoId: "uuid"
    s3Path: "library/iso-uuid.iso"
status:
  state: Pending | Creating | Stopped | Running | Error
  kubevirtVmName: "troshka-vm-{id[:8]}"
  vncEndpoint: "wss://..."
  ipAddresses:
    "nic-uuid-1": "10.0.0.10"
  goldenPvcReady: true  # image imported from S3
  message: ""
```

### Operator Reconciliation

The operator uses kopf (Python Kubernetes Operator Framework). Three handlers:

#### TroshkaProject handler

Watches `TroshkaProject` CRs. On create:

1. Parse `spec.topology` (same parsing logic as `deploy_service.py`)
2. Create `TroshkaNetwork` CRs for each network in topology
3. Wait for all networks to report `status.ready: true`
4. Create `TroshkaVM` CRs for each VM in topology (respecting `startOrder`)
5. Update `status.phase` through the lifecycle
6. Aggregate `status.vmStates` from child `TroshkaVM` status

On delete: K8s garbage collection via ownerReferences cascades to all children. Finalizer on TroshkaProject handles cleanup of non-owned resources (OCP Routes, DNS records, S3 artifacts).

#### TroshkaNetwork handler

Watches `TroshkaNetwork` CRs. On create:

1. Create `NetworkAttachmentDefinition` (OVN layer2 secondary network)
2. Create dnsmasq Pod (DHCP/DNS, attached to NAD via Multus)
3. If `spec.externalAccess`: create gateway Pod (nftables NAT, attached to NAD + pod network)
4. If `spec.pxeConfig.enabled`: run PXE init Job (extract kernel/initrd from ISO), extend dnsmasq config with TFTP
5. Update `status.ready` when all pods are Running

On delete: ownerReferences cascade Pod/NAD deletion.

#### TroshkaVM handler

Watches `TroshkaVM` CRs. On create:

1. For each disk with `libraryImage` or `patternImage`:
   a. Check if golden PVC exists (named `golden-{s3-path-hash}`)
   b. If not: create DataVolume importing from S3
   c. Clone golden PVC → VM disk PVC (Ceph RBD instant clone)
2. For blank disks: create empty PVC at specified size
3. If `spec.cdrom`: import ISO via DataVolume
4. Create cloud-init Secret from `spec.cloudInit`
5. If guestfish operations needed (cert cleanup for patterns): run guestfish Job, wait for completion
6. Create KubeVirt `VirtualMachine` CR with:
   - Disk PVCs as `persistentVolumeClaim` volumes
   - NICs as Multus interfaces referencing `TroshkaNetwork` NADs
   - Cloud-init Secret as `cloudInitNoCloud` volume
   - Firmware, machine type, SMBIOS UUID from spec
7. If `spec.bmcEnabled`: create/update BMC Pod for project (shared across VMs)
8. If `spec.powerOnAtDeploy`: set `spec.running: true`
9. Watch `VirtualMachineInstance` status for state updates

On delete: KubeVirt VM deletion cascades to VMI. PVCs deleted via ownerReferences.

### Networking Implementation

#### L2 Isolation — OVN Secondary Networks

Each TroshkaNetwork creates a `NetworkAttachmentDefinition`:

```yaml
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: net-{id[:8]}-nad
spec:
  config: |
    {
      "cniVersion": "0.3.1",
      "name": "net-{id[:8]}",
      "type": "ovn-k8s-cni-overlay",
      "topology": "layer2",
      "subnets": "10.0.0.0/24",
      "excludeSubnets": "10.0.0.1/32"
    }
```

- OVN layer2 topology creates an isolated logical switch per NAD
- Same CIDRs across projects work because each logical switch is independent
- VMs attach via Multus annotations on the KubeVirt VM spec
- OVN handles the overlay across cluster nodes automatically (no manual VXLAN peering)

#### dnsmasq Pod

One per project, attached to all project networks:

- DHCP serving all configured subnets
- Static leases from topology MAC/IP assignments
- DNS forwarding
- Optional TFTP for PXE boot
- Config generated from TroshkaNetwork specs
- Managed by operator, restarted on crash

#### Gateway Pod

One per project (when external access is enabled):

- Attached to all project networks + pod network
- nftables masquerade for outbound NAT
- DNAT rules for inbound port forwards
- IP forwarding enabled
- Same nftables logic as current gateway netns, containerized

#### External Access — OCP Routes

For port forwards (443, 80, 6443, etc.):

1. Operator creates a ClusterIP Service pointing at the gateway Pod's DNAT port
2. Operator creates an OCP Route: `{vm-name}-{port}.apps.{cluster-domain}`
3. Gateway Pod has nftables DNAT: Route port → VM internal IP:port

Same pattern as the current `create_route_access` in `ocpvirt.py`, but the DNAT target is a gateway Pod instead of a host netns.

### BMC / Redfish Emulation

#### Custom sushy KubeVirt Driver

New Python module: ~200-300 lines implementing `AbstractSystemsDriver`.

Methods:
- `get_power_state(identity)` → `GET VirtualMachineInstance` → map `status.phase` to Redfish power state
- `set_power_state(identity, state)` → `PATCH VirtualMachine` (running: true/false) or subresource API
- `get_boot_device(identity)` → read VM spec boot order
- `set_boot_device(identity, device)` → `PATCH VirtualMachine` boot order
- `get_boot_mode(identity)` → read VM spec firmware
- `set_boot_mode(identity, mode)` → `PATCH VirtualMachine` firmware
- `insert_image(identity, uri)` → add cdrom DataVolume to VM
- `eject_image(identity)` → remove cdrom from VM

The driver uses the kubernetes Python client with a ServiceAccount scoped to the project namespace.

#### BMC Pod

One per project (when any VM has `bmcEnabled: true`):

- Runs sushy-emulator with the KubeVirt driver
- Attached to the BMC network NAD
- htpasswd credentials from topology (stored in Secret)
- SMBIOS UUIDs (`spec.domain.firmware.uuid`) on VMs ensure Redfish identity matches

#### IPMI (vbmc)

Optional — ACM/ZTP uses Redfish exclusively. If needed, a similar KubeVirt driver for virtualbmc can be written. Recommend deferring IPMI in favor of Redfish-only for the initial implementation.

### PXE Boot

#### PXE Init Job

When a network has `pxeConfig.enabled`:

1. Operator creates a DataVolume importing the ISO from S3 (or references existing golden PVC)
2. Operator runs a PXE init Job:
   - Mounts ISO PVC (read-only)
   - Extracts kernel, initrd using `isoinfo` (same detection logic as current troshkad)
   - Writes extracted files + pxelinux config to an emptyDir or shared PVC
3. dnsmasq Pod is configured with TFTP pointing at extracted files
4. A lightweight HTTP server (Python) serves the ISO mount as install source

#### VM PXE Boot

KubeVirt VMs with PXE boot get `interfaces[].bootOrder: 1` so they attempt network boot first. The dnsmasq Pod serves the PXE response on the L2 network.

### VNC Console

#### VNC Proxy Pod

One per project, managed by operator:

- Receives WebSocket connections from browsers (noVNC protocol)
- Proxies to KubeVirt VNC subresource API: `POST /apis/subresources.kubevirt.io/v1/namespaces/{ns}/virtualmachineinstances/{name}/vnc`
- Authenticates to K8s API via ServiceAccount
- Exposed via OCP Route: `{instance-id}.{console-base-domain}` (same URL pattern as current)

The proxy Pod replaces `troshka-vncd`. JWT-based auth from the browser works the same way — the backend issues a JWT, the proxy validates it before opening the VNC connection.

### Pattern Operations

#### Pattern Capture (Save)

1. Backend sets project status to "capturing"
2. Provider driver creates/patches TroshkaProject CR with capture intent
3. Operator stops all VMs (`spec.running: false`)
4. For each VM disk PVC:
   a. Create `VolumeSnapshot` (Ceph RBD instant snapshot)
   b. Create export Job:
      - Creates temp PVC from VolumeSnapshot
      - Runs `qemu-img convert` (raw → qcow2)
      - Uploads qcow2 to S3 (`patterns/{pattern_id}/{disk_id}.qcow2`)
      - Cleans up temp PVC
5. Operator updates TroshkaProject status with S3 paths and completion
6. Backend records pattern in DB

#### Pattern Deploy

1. Backend creates TroshkaProject CR with topology from pattern
2. Operator processes TroshkaVM CRs:
   a. Check for golden PVC (`golden-{s3-path-hash}`) in cluster
   b. If missing: create DataVolume importing from S3 (one-time cache fill)
   c. Clone golden PVC → VM disk PVC (Ceph RBD instant clone)
3. Rest of deploy proceeds normally

Golden PVC caching means the first deploy of a pattern downloads from S3, subsequent deploys clone instantly. Golden PVCs are named deterministically: `golden-{sha256(s3_path)[:16]}` in a shared `troshka-cache` namespace, ensuring collision-free names and cross-project reuse.

#### Guestfish (Offline Disk Modification)

Used for cert cleanup on pattern-deployed OCP nodes:

1. Operator ensures VM is stopped (no VMI exists)
2. Creates guestfish Job:
   - Mounts VM disk PVC (RWO, safe because VM is stopped)
   - Runs guestfish commands from TroshkaVM spec
   - Exits
3. Operator starts VM after Job completes

### Containers & Pods

Regular K8s Pods. The operator creates them directly — no podman, no troshkad container endpoints.

- Single containers: one Pod per container node in topology
- Pod groups (`isPod: true`): one Pod with multiple containers + init containers
- Attached to project networks via Multus (same as VMs)
- Veth networking is handled by Multus/OVN, same L2 segment as VMs

### Virtual Host — Backend Compatibility

One Host record per `kubevirt` provider:

```
host.host_type = "kubevirt-cluster"
host.instance_id = cluster API URL
host.ip_address = cluster API endpoint
host.agent_status = "connected" (if K8s API reachable)
host.agent_token = ServiceAccount token
host.total_vcpus = sum(node.allocatable.cpu)
host.total_ram_mb = sum(node.allocatable.memory)
host.used_vcpus = sum(kubevirt VM cpu requests)
host.used_ram_mb = sum(kubevirt VM memory requests)
host.storage_size_gb = PV capacity in storage class
```

Health poller queries K8s API instead of troshkad `/health` for this host type.

Project placement selects the virtual host (only one per provider). KubeVirt handles actual node scheduling.

### Provider Driver (`kubevirt.py`)

Implements the 18-method `ProviderDriver` interface:

| Method | Implementation |
|--------|----------------|
| `provision_host` | Auto-creates virtual host record (no actual provisioning) |
| `terminate_host` | Deletes virtual host record |
| `get_host_status` | Queries K8s API for cluster health |
| `resize_host` | No-op (cluster capacity is fixed) |
| `extend_host_storage` | No-op |
| `setup_console` | Creates Route wildcard or DNS zone |
| `create_console_record` | Creates Route for VNC proxy Pod |
| `delete_console_record` | Deletes Route |
| `delete_console` | Tears down all console Routes |
| `get_host_powerstate` | Always "running" (cluster doesn't power off) |
| `start_host` / `stop_host` | No-op |
| `allocate_eip` | Creates LoadBalancer Service (MetalLB) |
| `associate_eip` | No-op (LB is already associated) |
| `release_eip` | Deletes LoadBalancer Service |
| `create_route_access` | Creates OCP Route + Service for port forward |
| `delete_route_access` | Deletes Routes/Services by label |

Additionally, the driver exposes methods for the deploy pipeline:

- `deploy_project(project_id, topology)` → creates `TroshkaProject` CR
- `destroy_project(project_id)` → deletes `TroshkaProject` CR
- `get_project_status(project_id)` → reads CR `.status`
- `get_vm_states(project_id)` → reads CR `.status.vmStates`

### Namespace Strategy

- **Per-project namespace:** `troshka-{project_id[:8]}` — all project resources (VMs, networks, helper pods, secrets) live here. Provides RBAC isolation and clean cascade deletion.
- **Shared cache namespace:** `troshka-cache` — golden PVCs for disk images. Shared across projects so the same pattern image is downloaded once.
- **Operator namespace:** `troshka-operator` — operator Deployment, ServiceAccount, RBAC.
- Operator has ClusterRole to manage resources across project namespaces and the cache namespace.
- Project namespace is created by the operator on TroshkaProject creation, deleted on TroshkaProject deletion (via finalizer).

### Cluster Prerequisites

- OpenShift 4.14+
- OpenShift Virtualization (KubeVirt + CDI)
- ODF (OpenShift Data Foundation) with Ceph RBD and CephFS
- OVN-Kubernetes with secondary network support
- Multus CNI (included with OCP)
- MetalLB (for LoadBalancer Services, if EIPs are needed)

### New Components to Build

| Component | Language | Size estimate | Description |
|-----------|----------|---------------|-------------|
| Troshka operator | Python (kopf) | ~2000-3000 lines | CRD reconciliation, helper pod management |
| sushy KubeVirt driver | Python | ~300 lines | Redfish → KubeVirt API translation |
| VNC proxy | Python | ~200 lines | WebSocket relay to KubeVirt VNC API |
| `troshka-tools` container image | Dockerfile | ~50 lines | qemu-img, guestfish, aws-cli, isoinfo |
| `kubevirt.py` provider driver | Python | ~500 lines | Backend provider interface for K8s |
| CRD manifests | YAML | ~200 lines | TroshkaProject, TroshkaNetwork, TroshkaVM |

### What Does NOT Change

- Backend data model (Project, Pattern, Library, User, Provider, Host models)
- Frontend (no UI changes — provider type selector gets a new option)
- Topology JSONB format
- Pattern S3 storage format
- Template import/export
- Authentication and permissions
- WebSocket pub/sub (driver publishes same events)
- All existing providers (ec2, ocpvirt, gcp, azure)

### Risks & Open Questions

1. **OVN secondary network maturity** — layer2 topology is GA in OCP 4.14+ but complex topologies (multiple NADs per pod, mixed with masquerade) may have edge cases
2. **Ceph RBD clone performance at scale** — should be fine for 10s of clones, untested at 100s
3. **Golden PVC lifecycle** — needs a garbage collection strategy for cached images no longer referenced by any pattern or library item
4. **KubeVirt VNC API stability** — subresource API is stable but WebSocket behavior under load is less tested
5. **sushy KubeVirt driver** — custom code with no upstream equivalent; if sushy-tools changes its driver interface, we need to update
6. **IPMI (vbmc)** — deferred to Redfish-only initially. If IPMI is needed, a similar driver for vbmc can be written later
7. **Serial console exec** — KubeVirt has `virtctl console` (WebSocket). The backend's exec API would need a branch to use this instead of troshkad's serial endpoint. Lower priority — SSH exec still works.
