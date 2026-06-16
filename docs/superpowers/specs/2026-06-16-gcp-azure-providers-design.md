# GCP and Azure Provider Drivers — Design Spec

## Summary

Add GCP and Azure as provider types to Troshka, including managed NFS storage, console TLS, EIPs, and storage auto-extend. Implementation follows Approach A: GCP first (networking model closer to EC2), then Azure. Both implement the existing `ProviderDriver` base class (16 methods) with no changes to the interface.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Implementation order | GCP first, then Azure | GCP networking closer to EC2; validates shared plumbing first |
| Azure NFS service | Azure Files NFS (Premium v2) | ~$0.10/GiB/month, 32 GiB min, standard ARM API. NetApp Files is overkill ($0.15-0.39/GiB, 1 TiB min) |
| GCP NFS service | Filestore Zonal | ~$0.12/GiB/month, single-zone, custom perf. Closest to FSx OpenZFS |
| Instance types | Esv5 (Azure), N2-highmem (GCP) | 8 GiB/vCPU — RAM-heavy for nested virt density |
| Default instance | `Standard_E32s_v5` / `n2-highmem-32` | 32 vCPU / 256 GiB — comparable to current EC2 usage |
| RHEL images | BYOS Gold Images via Red Hat Cloud Access | No RHEL premium on cloud bill; PAYG fallback if not enrolled |
| Console TLS | Cloud DNS + certbot DNS-01 (mirror EC2 pattern) | Same proven flow, different cloud DNS APIs |
| Provider model | Dedicated columns per cloud | Queryable, type-safe, matches existing EC2 pattern |
| Network setup | One-click "Setup Network" button | Same UX as EC2 "Setup VPC", cloud-appropriate plumbing |
| Auto-extend | Day one for both NFS and host disks | Low effort given existing pattern; running out of storage is painful |
| Admin UI | Same layout, conditional fields per provider type | No new pages; type dropdown drives field rendering |
| Feature parity | Full parity with EC2 | All 16 ProviderDriver methods, storage pools, console, EIPs |

## Provider Model Changes

New columns on `providers` table:

```
# GCP-specific
gcp_project_id          String(100)   — GCP project where resources live
gcp_network_id          String(255)   — VPC network self-link
gcp_subnet_id           String(255)   — subnet self-link
gcp_firewall_policy     String(255)   — firewall policy name or self-link
gcp_zone                String(50)    — default zone (e.g., us-central1-a)

# Azure-specific
azure_subscription_id   String(50)    — Azure subscription UUID
azure_resource_group    String(100)   — resource group name
azure_vnet_id           String(255)   — VNet resource ID
azure_subnet_id         String(255)   — subnet resource ID
azure_nsg_id            String(255)   — Network Security Group resource ID
azure_location          String(50)    — Azure region (e.g., eastus)
```

Credentials stored in existing `credentials` JSON blob:
- **GCP**: `{"service_account_json": {...}}`
- **Azure**: `{"client_id": "...", "client_secret": "...", "tenant_id": "...", "subscription_id": "..."}`

`type` field gains values: `"gcp"`, `"azure"`.

`default_ami` column reused for GCP image self-link / Azure image URN.

Driver factory in `providers/__init__.py` adds two branches.

## StoragePool Model Changes

New columns on `storage_pools` table:

```
# GCP Filestore
filestore_instance_id    String(255)   — Filestore instance name/self-link
filestore_ip             String(45)    — NFS mount IP
filestore_share_name     String(100)   — file share name (default "troshka")
filestore_tier           String(20)    — "ZONAL" default
filestore_capacity_gb    Integer       — provisioned capacity

# Azure Files NFS
azure_storage_account    String(100)   — storage account name
azure_file_share_name    String(100)   — NFS share name
azure_file_share_url     String(500)   — mount endpoint
azure_files_capacity_gb  Integer       — provisioned capacity (GiB)
azure_files_iops         Integer       — provisioned IOPS (v2 billing)
azure_files_throughput   Integer       — provisioned throughput MiB/s
```

`mode` field gains values: `"shared-filestore"`, `"shared-azure-files"`.

### Filestore Creation Flow

1. `filestore.projects.locations.instances.create()` — tier=ZONAL, capacity, network
2. Poll until state=READY
3. Extract `networks[0].ipAddresses[0]` as mount IP
4. Mount: `{ip}:/{share_name}`

### Azure Files NFS Creation Flow

1. Create Storage Account — `kind=FileStorage`, `sku=Premium_LRS`, `enable_nfs_v3=True`
2. Create NFS file share with provisioned capacity
3. Create private endpoint in VNet (required for Azure Files NFS)
4. Mount: `{account}.file.core.windows.net:/{account}/{share}`

### Auto-Extend

- `extend_pool_filestore()` — `instances.patch(fileCapacityGb=new_size)`, no cooldown
- `extend_pool_azure_files()` — update share quota, no cooldown
- Host disk extend: GCP `disks.resize()`, Azure `disks.begin_update()`

NFS mount on hosts is identical to today — cloud-init gets `nfs_server` and `nfs_path`, mounts at `/var/lib/troshka/shared` with `nfsvers=4.1,nconnect=16,hard,_netdev`.

## Curated Instance Types

### Azure — Esv5 Series (Memory Optimized, 8 GiB/vCPU)

| Size | vCPUs | RAM | $/hr |
|---|---|---|---|
| `Standard_E4s_v5` | 4 | 32 GiB | $0.25 |
| `Standard_E8s_v5` | 8 | 64 GiB | $0.50 |
| `Standard_E16s_v5` | 16 | 128 GiB | $1.01 |
| **`Standard_E32s_v5`** (default) | **32** | **256 GiB** | **$2.02** |
| `Standard_E48s_v5` | 48 | 384 GiB | $3.02 |
| `Standard_E64s_v5` | 64 | 512 GiB | $4.03 |
| `Standard_E96s_v5` | 96 | 672 GiB | $6.05 |

### GCP — N2-highmem Series (8 GiB/vCPU, Intel, nested virt supported)

| Size | vCPUs | RAM | $/hr |
|---|---|---|---|
| `n2-highmem-4` | 4 | 32 GiB | $0.27 |
| `n2-highmem-8` | 8 | 64 GiB | $0.53 |
| `n2-highmem-16` | 16 | 128 GiB | $1.07 |
| **`n2-highmem-32`** (default) | **32** | **256 GiB** | **$2.13** |
| `n2-highmem-48` | 48 | 384 GiB | $3.20 |
| `n2-highmem-64` | 64 | 512 GiB | $4.27 |
| `n2-highmem-80` | 80 | 640 GiB | $5.34 |

## RHEL Images

BYOS Gold Images via Red Hat Cloud Access (no RHEL premium on cloud bill).

| | AWS | GCP | Azure |
|---|---|---|---|
| **BYOS source** | Private AMIs (owner `309956199498`) | `rhel-byos-cloud` project | `redhat` publisher, `rhel-byos` offer |
| **Discovery API** | `describe_images(Owners=[...])` | `images.list(project="rhel-byos-cloud")` | `virtual_machine_images.list(publisher="redhat", offer="rhel-byos")` |
| **PAYG fallback** | Standard RHEL AMIs | `rhel-cloud` project | `RedHat:RHEL:9_5:latest` |
| **RHEL 9** | Available | Available | Available |
| **RHEL 10** | Available | Available | Available |
| **Updates** | RHUI (automatic) | RHSM registration | RHSM registration |

Azure requires marketplace terms acceptance on first use: `marketplace_agreements.create()`.

GCP requires Google Group enrollment in Red Hat Cloud Access portal (one-time setup).

## GCP Driver (`gcp.py`)

SDK: `google-cloud-compute`, `google-cloud-filestore`, `google-cloud-dns`.

### Host Lifecycle

**`provision_host()`:**
1. Generate SSH keypair in-process
2. Create instance:
   - Machine type from curated N2-highmem list (default `n2-highmem-32`)
   - Boot disk: RHEL BYOS image, 50 GB
   - Data disk: persistent SSD, `storage_size_gb`
   - `advancedMachineFeatures.enableNestedVirtualization=true`
   - SSH key via instance metadata
   - Cloud-init YAML via metadata
   - NFS mount in cloud-init if shared pool
3. Poll until RUNNING
4. Return dict with IPs, specs, private_key

**`terminate_host()`:** Delete instance + data disk.

**`resize_host()`:** Stop → `setMachineType()` → Start (GCP requires stop).

**`get_host_status()`:** `instances.get()` → map RUNNING/TERMINATED/STAGING.

### Power Management

- `start_host()`: `instances.start()`
- `stop_host()`: `instances.stop()`

### Console DNS

- `setup_console()`: Create Cloud DNS zone, service account with `dns.admin` role
- `create_console_record()`: `changes.create()` with A record
- `delete_console_record()`: `changes.create()` with deletion
- Certbot uses `certbot-dns-google` plugin with service account JSON

### EIP Management

- `allocate_eip()`: `addresses.insert()` (static external IP)
- `associate_eip()`: Add access config to NIC
- `release_eip()`: `addresses.delete()`
- `update_eip_ports()`: Update firewall rules

### Network Setup (admin API)

1. Create VPC network (custom mode)
2. Create subnet in region (`10.100.1.0/24`)
3. Create firewall rules (SSH 22, console 443, troshkad 31337, VXLAN UDP 4789)
4. Store network/subnet/firewall IDs on provider

### Image Discovery (admin API)

- `images.list(project="rhel-byos-cloud")` — RHEL 9/10 x86_64
- Fallback: `images.list(project="rhel-cloud")` for PAYG

## Azure Driver (`azure.py`)

SDK: `azure-identity`, `azure-mgmt-compute`, `azure-mgmt-network`, `azure-mgmt-storage`, `azure-mgmt-dns`, `azure-mgmt-marketplaceordering`.

### Host Lifecycle

**`provision_host()`:**
1. Generate SSH keypair in-process
2. Create NIC with public IP in subnet + NSG
3. Create VM:
   - Size from curated Esv5 list (default `Standard_E32s_v5`)
   - OS disk: RHEL BYOS image URN, 50 GB
   - Data disk: Premium SSD managed disk, `storage_size_gb`
   - Cloud-init via `custom_data` (base64)
   - NFS mount in cloud-init if shared pool
4. Accept marketplace terms if first BYOS use
5. Poll until provisioning succeeded + power state running
6. Return dict with IPs, specs, private_key

**`terminate_host()`:** Delete VM → delete OS disk, data disk, NIC, public IP (Azure doesn't auto-delete dependents).

**`resize_host()`:** Attempt hot-resize via `virtual_machines.update()`; fall back to deallocate → resize → start.

**`get_host_status()`:** `virtual_machines.instance_view()` → parse power state.

### Power Management

- `start_host()`: `virtual_machines.begin_start()`
- `stop_host()`: `virtual_machines.begin_deallocate()` (releases compute billing)

### Console DNS

- `setup_console()`: Create Azure DNS zone
- `create_console_record()`: `record_sets.create_or_update()` A record
- `delete_console_record()`: `record_sets.delete()`
- Certbot uses `certbot-dns-azure` plugin

### EIP Management

- `allocate_eip()`: `public_ip_addresses.begin_create_or_update()` — Standard SKU, static
- `associate_eip()`: Secondary NIC IP config + associate public IP
- `release_eip()`: Delete public IP resource
- `update_eip_ports()`: Update NSG rules

### Network Setup (admin API)

1. Create Resource Group (`troshka-rg`)
2. Create VNet (`10.100.0.0/16`) with subnet (`10.100.1.0/24`)
3. Create NSG with rules (SSH, console, troshkad, VXLAN)
4. Associate NSG with subnet
5. Store resource group, VNet, subnet, NSG IDs on provider

### Image Discovery (admin API)

- `virtual_machine_images.list(publisher="redhat", offer="rhel-byos")` — RHEL 9/10
- Fallback: `offer="RHEL"` for PAYG
- Returns URNs: `Publisher:Offer:Sku:Version`

### Azure-Specific Quirks

- NICs and public IPs are independent resources (not embedded in VM)
- Terminate must clean up dependents in correct order (VM → disk → NIC → IP)
- Deallocate (not stop) to release compute billing
- Marketplace terms acceptance required for BYOS images
- Resource Group acts as blast radius boundary

## Dependencies

```
# GCP (add to requirements.txt)
google-cloud-compute>=1.20.0
google-cloud-filestore>=1.10.0
google-cloud-dns>=0.35.0

# Azure (add to requirements.txt)
azure-identity>=1.17.0
azure-mgmt-compute>=32.0.0
azure-mgmt-network>=26.0.0
azure-mgmt-storage>=21.0.0
azure-mgmt-dns>=8.2.0
azure-mgmt-marketplaceordering>=1.1.0
```

## Cloud-Init

All clouds use the same logical payload. Delivery mechanism differs:

| | EC2 | GCP | Azure |
|---|---|---|---|
| Mechanism | `UserData` (base64) | `metadata.startup-script` or cloud-init | `custom_data` (base64) |
| Size limit | 16 KB | 256 KB | 64 KB |
| Format | cloud-init YAML | cloud-init YAML | cloud-init YAML |

Content (identical across all):
- Install `qemu-kvm`, `libvirt`, `dnsmasq`, `nftables`, `python3`
- Mount data disk to `/var/lib/troshka`
- Mount NFS if shared pool
- Kernel tuning (overcommit, KSM)

Data disk device paths:
- EC2: `/dev/nvme1n1` (Nitro)
- GCP: `/dev/sdb`
- Azure: `/dev/sdc` (LUN 0)

## Troshkad — No Changes

The host agent is provider-agnostic. All 31 endpoints work identically across clouds. Disk paths, NFS mount points, libvirt, VXLAN mesh — all the same. Only the provisioning and certbot DNS auth differ, both handled outside troshkad.

## Agent Deployer — Minor Changes

- SSH user: `ec2-user` (EC2), `cloud-user` (OCPVirt), `troshka` (GCP — set via SSH key metadata username), `troshka` (Azure — set via cloud-init `admin_username`)
- Data disk device path: `/dev/nvme1n1` (EC2 Nitro), `/dev/sdb` (GCP — second disk is always sdb), `/dev/disk/azure/scsi1/lun0` (Azure — symlink is stable regardless of device letter assignment)
- Otherwise identical.

## Frontend Changes

No new pages. Conditional rendering based on `provider.type`:

**Provider form:** Type dropdown with EC2/OCPVirt/GCP/Azure. Credential fields swap per type. Region field universal.

**Provider detail:** Button labels adapt ("Setup Network" instead of "Setup VPC"). Network info section shows cloud-appropriate field names.

**Host provisioning:** Instance type input free text, default pre-filled from curated list.

**Storage pool creation:** Mode dropdown adds Filestore (GCP) and Azure Files NFS. Cloud-specific fields (tier, IOPS, throughput).

**No changes:** Canvas, deploy flow, console, patterns, library, health poller, GC — all provider-agnostic.

## Implementation Phases

### Phase 1: Shared Foundation
1. Alembic migration — all new Provider + StoragePool columns
2. Update models with new columns
3. Driver factory — add `gcp` and `azure` branches
4. Frontend — provider type dropdown, conditional fields
5. Image discovery endpoint — generalize per provider type

### Phase 2: GCP Driver (end-to-end)
1. `gcp.py` — all 16 ProviderDriver methods
2. Network setup endpoint
3. Console setup (Cloud DNS + service account)
4. Filestore pool creation + extend
5. Host disk extend for GCP persistent disk
6. Agent deployer updates
7. Frontend — GCP pool creation fields
8. E2E test: provision → deploy agent → deploy project → console

### Phase 3: Azure Driver (end-to-end)
1. `azure.py` — all 16 ProviderDriver methods
2. Network setup (Resource Group + VNet + NSG)
3. Console setup (Azure DNS + certbot-dns-azure)
4. Azure Files NFS pool creation + extend
5. Host disk extend for Azure managed disk
6. Marketplace terms acceptance
7. Agent deployer updates
8. Frontend — Azure pool creation fields (IOPS/throughput)
9. E2E test

## Out of Scope

- Custom machine types on GCP
- Spot/preemptible instances
- Multi-region per provider
- Cross-cloud migration
- Azure Dedicated Hosts / GCP Sole-Tenant Nodes

## Files Touched (Estimated)

| Area | Files | Nature |
|---|---|---|
| Models | `provider.py`, `storage_pool.py` | Add columns |
| Migration | New alembic revision | Schema changes |
| Drivers | New `gcp.py` (~800 lines), new `azure.py` (~800 lines) | New files |
| Driver factory | `providers/__init__.py` | 4 lines |
| Storage pools | `storage_pool_service.py`, `storage_extend.py` | Add functions |
| Admin API | `providers.py` | Network setup + image discovery |
| Agent deploy | `agent_deployer.py` | SSH user + disk path |
| Frontend | `admin/hosts/page.tsx`, provider pages | Conditional rendering |
| Dependencies | `requirements.txt` | GCP + Azure SDK packages |
