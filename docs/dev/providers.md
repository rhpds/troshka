# Provider Setup Reference

> Extracted from the top-level `CLAUDE.md` to keep it lean. Read this file when working on the topics below.

### AWS Provider Setup
- IAM user: `troshka` with inline policy `troshka-policy`
- Credentials stored in `~/secrets/troshka-aws.env`
- Required IAM permissions:
  - **EC2**: RunInstances, TerminateInstances, StopInstances, StartInstances, RebootInstances, ModifyInstanceAttribute, Describe{Instances,InstanceTypes,Images,Vpcs,Subnets,AvailabilityZones}, CreateKeyPair, DeleteKeyPair, CreateTags
  - **EBS**: CreateVolume, DeleteVolume, AttachVolume, DetachVolume, DescribeVolumes, ModifyVolume
  - **VPC**: Create/Delete/Modify{Vpc,Subnet,VpcAttribute,SubnetAttribute}, Create/Delete/Attach/Detach InternetGateway, Create/DeleteRoute, Describe{RouteTables,InternetGateways}, AssociateRouteTable
  - **Security Groups**: Create/Delete, Describe{SecurityGroups,SecurityGroupRules}, Authorize/RevokeSecurityGroupIngress
  - **Elastic IPs**: Allocate/Release/Associate/Disassociate Address, DescribeAddresses, AssignPrivateIpAddresses, UnassignPrivateIpAddresses
  - **FSx**: CreateFileSystem, DeleteFileSystem, DescribeFileSystems, UpdateFileSystem, CreateVolume, DeleteVolume, DescribeVolumes, UpdateVolume, TagResource, UntagResource, ListTagsForResource
  - **VPC Endpoints**: CreateVpcEndpoint, DeleteVpcEndpoints, DescribeVpcEndpoints, ModifyVpcEndpoint
  - **IAM** (one-time): CreateServiceLinkedRole for fsx.amazonaws.com
  - **S3**: PutObject, GetObject, DeleteObject, HeadObject, ListBucket on `troshka-images`
- IAM policy is a managed policy `troshka-policy` — source of truth at `infra/iam-policy.json`
- VPC setup creates subnets in all AZs — provisioner retries across AZs if instance type not supported
- VPC setup creates an S3 Gateway Endpoint — keeps S3 traffic on AWS private network (free, no NAT fees)
- Provisioner never falls back to default VPC — requires explicit VPC setup
- `Setup VPC` auto-creates a troshka-managed VPC if none exists (tagged `ManagedBy: troshka`)
- VPC discovery only lists troshka-managed VPCs, not all VPCs in the account
### OCP Virt Provider Setup
- Provider type `ocpvirt` — creates nested-virt RHEL VMs on OpenShift Virtualization
- Troshkad runs identically inside KubeVirt VMs as in EC2 instances
- **Provider driver abstraction**: `src/backend/app/services/providers/` — `base.py` interface (16 methods), `ec2.py`, `ocpvirt.py`, `gcp.py`, `azure.py`
- All provisioner calls go through `get_provider_driver(provider)` dispatcher
- **Dev cluster**: `ocpvdev01.dal13.infra.demo.redhat.com` (AMD EPYC 7763, 256 vCPU / 1TB RAM per worker, nested virt enabled)
- **Service account**: `troshka` SA in `troshka` namespace with `troshka-provider` ClusterRole
- **Token**: `oc create token troshka -n troshka --duration=8760h` (1 year)
- **ClusterRole permissions** (least-privilege):
  - `kubevirt.io`: virtualmachines (CRUD + patch), virtualmachineinstances (get, list)
  - `cdi.kubevirt.io`: datavolumes (CRUD)
  - Core: services, PVCs (CRUD + patch), PVs (get, list), namespaces (get, create), nodes (get, list)
  - `route.openshift.io`: routes (CRUD + patch)
- **Storage**: Ceph-NFS via `ocs-storagecluster-ceph-nfs` storage class, ~2.7 TiB available on CephFS
- **Console**: OCP edge Routes (TLS terminated by OCP router), vncd runs with `--no-tls` flag on port 8080
- **Console Route annotation**: `haproxy.router.openshift.io/timeout: 3600s` required for WebSocket — without it HAProxy sends `Connection: Close` and consoles fail
- **Networking**: identical to AWS (VXLAN, nftables, netns) — all inside the host VM
- **EIPs**: not supported on OCP Virt — `externalAccess` toggle disabled for ocpvirt hosts
- **Resize**: not supported (KubeVirt requires stop → modify → start, disabled for now)
- **Package repo** (recommended for dedicated CI): instead of importing an 11 GB RHEL DVD ISO per provision, hosts can `dnf install` from an HTTP repo with basic auth. Defaults in `src/backend/config/config.yaml` under `ocpvirt.pkg_repo`; deploy infra with `deploy/ansible/pkg-repo.yaml`. Provider registration can override per cluster. See [`ocpvirt-package-repo.md`](ocpvirt-package-repo.md).
### KubeVirt Native Provider Setup
- Provider type `kubevirt` — creates KubeVirt VMs directly on OCP (no nested virt, no troshkad)
- **Architecture**: kopf-based operator manages CRDs (`TroshkaProject`, `TroshkaNetwork`, `TroshkaVM`) that reconcile into KubeVirt VMs, OVN networks, and helper pods
- **Provider driver**: `src/backend/app/services/providers/kubevirt.py` — thin layer creating/watching CRDs
- **Operator**: `src/operator/` — Python/kopf, deployed to `troshka-operator` namespace (configurable)
- **CRDs**: `src/operator/crds/` — TroshkaProject, TroshkaNetwork, TroshkaVM
- **Container images**: operator, dnsmasq, gateway, troshka-tools, sushy, vnc-proxy — built by CI, pushed to `quay.io/redhat-gpte/troshka-*`
- **Prerequisites**: OCP 4.14+, OpenShift Virtualization (KubeVirt + CDI), ODF (Ceph RBD + CephFS), OVN-Kubernetes secondary networks
- **RBAC**: uses same `troshka` SA as nested ocpvirt — `infra/ocpvirt-rbac.yaml` has all permissions for both provider types
- **RBAC escalation**: K8s prevents the SA from creating ClusterRoles with permissions it doesn't hold. An OCP admin must pre-apply the operator RBAC:
  ```bash
  oc apply -f src/operator/deploy/clusterrole.yaml
  oc apply -f src/operator/deploy/clusterrolebinding.yaml
  ```
- **Setup flow**: Create provider in admin UI → auto-creates virtual host → background thread deploys operator + CRDs → "Install Operator" button for repair/retry
- **Virtual host**: one Host record per provider with `host_type="kubevirt-cluster"` — represents cluster capacity, no SSH/agent
- **Networking**: OVN layer2 secondary networks (NADs) + dnsmasq Pod (DHCP/DNS) + gateway Pod (NAT) per project
- **NAD config**: must include `netAttachDefName: "namespace/name"` — OVN-K rejects pods without it
- **SCC**: custom `troshka-network-pods` SCC for NET_ADMIN/NET_RAW, `troshka-network` SA created per project namespace and patched into SCC
- **BMC**: sushy emulator Pod with custom KubeVirt driver (Redfish only, no IPMI), `troshka-bmc` SA created per project namespace
- **VNC console**: `vnc-proxy-{project}` Pod per project, relays noVNC WebSocket to KubeVirt VNC subresource API (`subresources.kubevirt.io/v1/.../vnc`)
- **VNC Route**: OCP edge-terminated Route with `haproxy.router.openshift.io/timeout: 3600s`, auto-generated hostname stored in TroshkaProject CR `status.consoleRoute`
- **VNC RBAC**: `troshka-vnc` SA per project namespace with Role granting `get` on `kubevirt.io` VMIs and `subresources.kubevirt.io` VMIs/vnc
- **VNC proxy image**: reads SA token from `/var/run/secrets/kubernetes.io/serviceaccount/token`, K8S_HOST from env vars
- **Patterns**: fully portable across providers — same topology JSONB, same S3 disk images (qcow2), CDI import → golden PVC → Ceph RBD clone
- **Golden PVC sizing**: reads qcow2 header (bytes 24-31) via S3 Range request for virtual size, headroom `max(size+10, size*1.2)`
- **Pattern capture**: VolumeSnapshot → export Job (qemu-img convert + S3 upload) — untested
- **UEFI SecureBoot**: must explicitly set `secureBoot: false` for plain UEFI — KubeVirt defaults to `true`, which requires SMM
- **Boot order**: `bootOrder` is a sibling of `disk:` on the disk entry, NOT nested inside `disk.disk`
- **VM state polling**: WS poller reads `vmStates` from TroshkaProject CR, maps by VM node UUID (not domain name), normalizes `Running`→`running`
- **Not supported**: clock backdating (no `virsh domtime` equivalent in KubeVirt), gateway NAT pod (TODO)
- **Operator workloads as Deployments**: dnsmasq, gateway, exec, BMC pods are all Deployments (not standalone Pods). Enables `oc rollout restart` for image updates. VNC proxy was already a Deployment. Legacy standalone Pods auto-cleaned on upgrade via `_cleanup_legacy_pod()`.
- **oc-exec via exec pod**: `_exec_oc` for kubevirt-cluster uses the exec pod directly with `KUBECONFIG=/root/.kube/config`. No bastion needed for simple `oc` commands. Shell pipelines fall through to bastion SSH.
### GCP Provider Setup
- Provider type `gcp` — creates nested-virt RHEL VMs on Google Compute Engine
- **Driver**: `src/backend/app/services/providers/gcp.py` (~800 lines, self-contained)
- **Dev project**: `troshka-rhdp` under `rhpds-apps` folder (809829662025), billing on RHPDS Master
- **Prerequisites**: pre-create a GCP project, enable Compute Engine + Cloud DNS APIs, create SA with Compute Admin + DNS Admin roles
- **Credentials**: `{"service_account_json": {...}}` — full service account key JSON
- **Instance types**: N2-highmem for hosts (nested virt), E2-standard for pattern buffers (no nested virt needed)
- **Org policy constraint**: `custom.denyCostlyMachineTypes` blocks "exotic" types — E2 and N2-standard work, N2-highmem may need an exception for host provisioning
- **Nested virt**: enabled via `advancedMachineFeatures.enableNestedVirtualization=True`, disabled for pattern buffer hosts
- **Network tags**: instances MUST have `troshka-host` tag for firewall rules to apply (SSH, console, agent, VXLAN)
- **Images**: currently using PAYG from `rhel-cloud` (repos work out of the box). BYOS from `rhel-byos-cloud` available but needs RHSM registration for package installs. Future: Red Hat Image Builder API for custom images with packages pre-installed.
- **Network setup**: "Setup Network" creates custom-mode VPC, subnet (`10.100.1.0/24`), firewall rules targeting `troshka-host` tag
- **Console**: Cloud DNS zone + `certbot-dns-google` plugin for Let's Encrypt TLS
- **EIPs**: GCP static external IPs, associated via access config on nic0
- **Shared storage**: not supported yet (Filestore/NetApp blocked by org policy). Use `local` pool mode with pattern buffer for pattern save.
- **SSH user**: `troshka` (set via instance metadata `ssh-keys`)
- **Data disk**: `/dev/sdb` (second attached persistent SSD)
- **Resize**: requires stop → `setMachineType()` → start (GCP limitation)
- **Pattern buffer**: uses `e2-standard-2` (allowed by org policy, no nested virt)
### Azure Provider Setup
- Provider type `azure` — creates nested-virt RHEL VMs on Azure
- **Driver**: `src/backend/app/services/providers/azure.py` (~880 lines, self-contained)
- **Prerequisites**: create service principal in Azure subscription, assign Contributor role on resource group
- **Credentials**: `{"tenant_id": "...", "client_id": "...", "client_secret": "...", "subscription_id": "..."}`
- **Instance types**: Esv5 series (8 GiB/vCPU, Intel, nested virt supported). Default: `Standard_E32s_v5` (32 vCPU / 256 GiB)
- **Nested virt**: supported natively on Esv5 series (no extra flag)
- **Images**: RHEL BYOS from `redhat` publisher, `rhel-byos` offer (marketplace terms acceptance required on first use), PAYG fallback from `RHEL` offer. Same BYOS repos issue as GCP — future: Red Hat Image Builder for custom images.
- **Network setup**: "Setup Network" creates Resource Group, VNet (`10.100.0.0/16`), subnet, NSG with rules
- **Console**: Azure DNS zone + `certbot-dns-azure` plugin for Let's Encrypt TLS
- **EIPs**: Azure public IPs (Standard SKU, static), associated via NIC IP config
- **Shared storage**: Azure Files NFS Premium v2 (`shared-azure-files` pool mode), ~$0.10/GiB/month, online resize, network ACL deny-all + mandatory private endpoint
- **SSH user**: `troshka` (set via `admin_username`)
- **Data disk**: `/dev/disk/azure/scsi1/lun0` (stable symlink for LUN 0)
- **Terminate cleanup**: must delete VM → OS disk → data disk → NIC → public IP in order (Azure doesn't auto-delete dependents)
- **Stop vs deallocate**: always use `begin_deallocate()` not `begin_power_off()` — deallocate releases compute billing
### Libvirt Provider Setup
- Provider type `libvirt` — "bring your own host": adopts an existing SSH-reachable Linux box (libvirt + nested virt already set up) instead of provisioning compute via a cloud API
- **Driver**: `src/backend/app/services/providers/libvirt.py` — `provision_host`/`terminate_host`/`get_host_status` only; Troshka never creates or destroys the underlying machine
- **Setup**: create the provider with `{"ssh_private_key": "..."}` credentials, then `POST /hosts` with `{"provider_id": ..., "ip_address": "...", "instance_type": "manual", "disk_gb": ...}` — see `infra/libvirt-host-image/commands.md` for a full walkthrough
- **Networking**: identical to AWS/OCP Virt (VXLAN, nftables, netns) — troshkad runs the same on the adopted host
- **EIPs**: no real cloud "Elastic IP" resource exists for a self-hosted box, so `allocate_eip`/`associate_eip` hand back the host's own `ip_address` as both the public and private IP. This reuses the same host-level nftables DNAT path as EC2 (`ip daddr <private_ip> tcp dport <extPort> dnat to <transit_ip>:<extPort>` in `_setup_host_port_forward_dnat`, `src/troshkad/troshkad.py`) — no transit-port indirection, so the gateway's literal "Ext Port" is the port you actually connect to at `host_ip:extPort`
- **Port-collision caveat**: every EIP on a libvirt host maps to that *same* shared address. If two projects on the same host pick the same Ext Port, `nft` will silently create two DNAT rules and only the first-matching one ever fires — there's no validation against this today, so pick distinct Ext Ports per host
- **Testing note**: `host_ip:extPort` is only reachable from a genuinely separate client (another machine, or the far end of an SSH tunnel). Linux routes locally-generated packets addressed to one of the host's own IPs straight to the local socket, bypassing the DNAT rule — so `ssh`-ing into the host and `curl`-ing its own IP from that same shell will not exercise the port forward
- **`max_eips`**: defaults to a non-zero cap (`DEFAULT_MAX_EIPS` in `libvirt.py`) on newly-provisioned hosts — it isn't a real hardware limit, just a safety cap on the shared address above. Hosts provisioned before this existed can be fixed via `PATCH /hosts/{id}` with `{"max_eips": N}`
### Red Hat Image Builder Integration
- Builds custom RHEL host images with all packages pre-installed (qemu-kvm, libvirt, etc.) via Red Hat Insights Image Builder API
- Eliminates RHSM registration at boot and PAYG image premium
- **User flow**: Settings page → save Red Hat offline token → Provider page → "Build Host Image" → wait ~15 min → image auto-set as `default_image`
- Offline token: get from https://access.redhat.com/management/api, stored encrypted on User model (Fernet, same as OCP pull secret)
- Service: `src/backend/app/services/image_builder_service.py` — token exchange, compose submission, polling, progress tracking
- API: `POST /providers/{id}/build-image`, `GET .../status`, `DELETE .../status`
- Background thread polls Red Hat API every 30s, auto-refreshes access token on 401
- Progress tracked in module-level `_build_progress` dict (lost on restart)
- **Azure one-time setup**: Image Builder's service principal (`b94bb246-b02c-4985-9c22-d44e66f657f4`) needs Contributor on the target resource group:
  ```bash
  az ad sp create --id b94bb246-b02c-4985-9c22-d44e66f657f4
  az role assignment create --assignee b94bb246-b02c-4985-9c22-d44e66f657f4 \
    --role Contributor --scope /subscriptions/{SUB_ID}/resourceGroups/{RG_NAME}
  ```
- **Azure image format**: managed image resource ID (`/subscriptions/.../images/...`), NOT marketplace URN — `_parse_image_urn()` handles both
- **GCP setup**: no manual steps — image built in Red Hat's project, shared with service account. `share_with_accounts` must use `serviceAccount:` prefix
- **GCP image format**: `projects/{red-hat-project}/global/images/{name}` — GCP driver handles cross-project image paths
- Pattern buffer hosts also use `default_image` — extra packages are harmless
