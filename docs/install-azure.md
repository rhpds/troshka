# Troshka Azure Setup Guide

This guide walks through setting up Troshka on Microsoft Azure for nested VM environment provisioning.

## Prerequisites

Before starting, ensure you have:

- Azure subscription with an active resource group (Troshka can create one, but pre-creating allows RBAC scoping)
- Azure CLI installed (`az` command available)
- Troshka backend running — see [Common Setup](install-common.md) for backend/frontend/database installation

## 1. Create Service Principal

Create a service principal with Contributor role scoped to your resource group:

```bash
az ad sp create-for-rbac \
  --name troshka \
  --role Contributor \
  --scopes /subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}
```

Save the output values:
- `appId` → client_id
- `password` → client_secret
- `tenant` → tenant_id

You will also need your subscription ID.

## 2. Accept Marketplace Terms

Accept the RHEL BYOS marketplace offer (one-time per subscription):

```bash
az vm image terms accept --publisher redhat --offer rhel-byos --plan rhel-lvm94-gen2
```

This allows Troshka to provision RHEL BYOS images without manual acceptance.

## 3. Create Provider in Troshka

Add the Azure provider:

1. Navigate to Admin → Providers
2. Click "Add Provider"
3. Fill in the form:
   - **Name**: azure-prod
   - **Type**: Azure
   - **Tenant ID**: YOUR_TENANT_ID
   - **Client ID**: YOUR_CLIENT_ID
   - **Client Secret**: YOUR_CLIENT_SECRET
   - **Subscription ID**: YOUR_SUBSCRIPTION_ID
4. Click "Save"
5. Click the "Test" button to verify connectivity

<details>
<summary>API equivalent</summary>

Add the Azure provider via the API:

```bash
curl -X POST http://localhost:8200/api/v1/providers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "azure-prod",
    "type": "azure",
    "credentials": {
      "tenant_id": "YOUR_TENANT_ID",
      "client_id": "YOUR_CLIENT_ID",
      "client_secret": "YOUR_CLIENT_SECRET",
      "subscription_id": "YOUR_SUBSCRIPTION_ID"
    }
  }'
```

Test connectivity:

```bash
curl -X POST http://localhost:8200/api/v1/providers/{PROVIDER_ID}/test \
  -H "Authorization: Bearer $TOKEN"
```

</details>

## 4. Network Setup

Create the Azure VNet, subnet, and NSG with required security rules:

1. Navigate to Admin → Providers
2. Locate your Azure provider
3. Click "Setup Network"
4. Fill in the form:
   - **Resource Group**: troshka-resources
   - **Location**: eastus
5. Click "Create"
6. Wait for the confirmation message

<details>
<summary>API equivalent</summary>

```bash
curl -X POST http://localhost:8200/api/v1/providers/{PROVIDER_ID}/create-network-azure \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "resource_group": "troshka-resources",
    "location": "eastus"
  }'
```

</details>

This creates:
- Resource Group (if it does not exist)
- Virtual Network (10.100.0.0/16)
- Subnet (10.100.1.0/24)
- Network Security Group with rules:
  - SSH (TCP 22)
  - Troshka agent (TCP 31337)
  - Console (TCP 443)
  - VXLAN overlay (UDP 4789)

## 5. Host Image Selection

Troshka supports two image options:

### Option A: RHEL BYOS (Default)

Uses the `redhat` publisher BYOS image (requires RHSM registration at boot for package installs):

1. Navigate to Admin → Providers
2. Locate your Azure provider
3. The default image `redhat:rhel-byos:rhel-lvm94-gen2:latest` is automatically set
4. You can change it via the image settings section if needed

<details>
<summary>API equivalent</summary>

Discover available images:

```bash
curl -X GET http://localhost:8200/api/v1/providers/{PROVIDER_ID}/discover-images-azure \
  -H "Authorization: Bearer $TOKEN"
```

Set as default:

```bash
curl -X POST http://localhost:8200/api/v1/providers/{PROVIDER_ID}/set-image \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "image_id": "redhat:rhel-byos:rhel-lvm94-gen2:latest"
  }'
```

</details>

### Option B: Red Hat Image Builder (Recommended)

Builds a custom RHEL managed image with all packages pre-installed (eliminates RHSM registration and PAYG premium).

**One-time Azure setup** — grant Contributor to Red Hat Image Builder service principal:

```bash
az ad sp create --id b94bb246-b02c-4985-9c22-d44e66f657f4

az role assignment create \
  --assignee b94bb246-b02c-4985-9c22-d44e66f657f4 \
  --role Contributor \
  --scope /subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}
```

Then in Troshka:
1. Navigate to Settings page
2. Save your Red Hat offline token (get from https://access.redhat.com/management/api)
3. Navigate to Providers page
4. Click "Build Host Image" on your Azure provider
5. Wait approximately 15 minutes for the build to complete
6. The new managed image will be auto-set as `default_image`

The resulting image resource ID format is `/subscriptions/.../resourceGroups/.../providers/Microsoft.Compute/images/{name}`.

## 6. Console Setup

Configure Azure DNS and Let's Encrypt TLS for VNC console access:

1. Navigate to Admin → Providers
2. Locate your Azure provider
3. Click "Setup Console"
4. Enter your base domain (e.g., `console.example.com`)
5. Click "Create"
6. Wait for the DNS zone to be created
7. Copy the returned nameservers from the collapsible section
8. Add the nameservers as NS records in your parent zone

<details>
<summary>API equivalent</summary>

```bash
curl -X POST http://localhost:8200/api/v1/providers/{PROVIDER_ID}/setup-console \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "base_domain": "console.example.com"
  }'
```

</details>

This creates an Azure DNS zone and returns nameservers for delegation. Add the returned nameservers as NS records in your parent zone.

The console uses the `certbot-dns-azure` plugin (authenticates via service principal credentials) to obtain Let's Encrypt certificates.

## 7. Provision First Host

Create a host instance:

1. Navigate to Admin → Providers
2. Locate your Azure provider
3. Click "Add Host"
4. Fill in the form:
   - **Instance Type**: Standard_E32s_v5 (or choose from recommended types below)
   - **Storage Size**: 500 GB
5. Click "Create"
6. Wait for the host to provision and connect (status shows "connected")

<details>
<summary>API equivalent</summary>

```bash
curl -X POST http://localhost:8200/api/v1/hosts \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "provider_id": "YOUR_PROVIDER_ID",
    "instance_type": "Standard_E32s_v5",
    "storage_size_gb": 500
  }'
```

</details>

### Recommended Instance Types

Troshka requires Esv5-series instances (Intel, 8 GiB RAM per vCPU, nested virtualization supported):

| Instance Type | vCPUs | RAM | Use Case |
|---------------|-------|-----|----------|
| Standard_E4s_v5 | 4 | 32 GB | Minimal dev/testing |
| Standard_E8s_v5 | 8 | 64 GB | Development |
| Standard_E16s_v5 | 16 | 128 GB | Small production |
| Standard_E32s_v5 | 32 | 256 GB | Production (default) |
| Standard_E48s_v5 | 48 | 384 GB | Large deployments |
| Standard_E64s_v5 | 64 | 512 GB | Large deployments |
| Standard_E96s_v5 | 96 | 672 GB | Largest deployments |

### Host Configuration Details

- **SSH user**: `troshka` (set via `admin_username`)
- **Data disk**: `/dev/disk/azure/scsi1/lun0` (stable symlink for LUN 0, formatted as ext4 and mounted at `/var/lib/troshka`)
- **OS disk**: 50 GB Premium SSD
- **Data disk**: Premium SSD (size specified in `storage_size_gb`)

### Important: Stop vs Deallocate

Azure has two stop modes:

- **Deallocate** (default in Troshka): releases compute billing, retains disk billing
- **Power off**: retains compute billing (not used by Troshka)

Troshka always uses `begin_deallocate()` to minimize cost. Verify deallocated state in the Azure Portal to avoid unexpected charges.

## 8. Shared Storage (Optional)

For multi-host deployments with live migration support, create an Azure Files NFS Premium v2 storage pool:

1. Navigate to Admin → Storage Pools
2. Click "Add Storage Pool"
3. Fill in the form:
   - **Name**: shared-pool
   - **Mode**: shared-azure-files
   - **Provider**: Select your Azure provider
4. Click "Create"
5. Wait for the storage account and file share to be created

<details>
<summary>API equivalent</summary>

```bash
curl -X POST http://localhost:8200/api/v1/storage-pools \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "shared-pool",
    "mode": "shared-azure-files",
    "provider_id": "YOUR_PROVIDER_ID"
  }'
```

</details>

This auto-creates:
- Storage account (Premium performance tier, NFS enabled)
- File share (NFS v4.1 protocol)
- Private endpoint (mandatory for security)
- Private DNS zone (`privatelink.file.core.windows.net`)

**Security**: Network ACL set to deny-all with mandatory private endpoint access (no public internet access).

**Pricing**: Approximately $0.10/GiB/month.

**Features**:
- Online resize (no downtime)
- LZ4 compression
- `nconnect=16` mount option for parallelism
- `cache=none,io=native` for VM disks

Hosts provisioned in this pool can live-migrate VMs to each other via libvirt TLS (mutual TLS with pool-level CA).

## 9. Verify Installation

Complete the following checklist:

- [ ] Provider test passes (`POST /providers/{id}/test`)
- [ ] Network created: VNet, subnet, and NSG visible in Azure Portal
- [ ] Host connected: status "connected" on Hosts page
- [ ] Console working: VNC console accessible via browser
- [ ] Deploy test: create project, add VM, deploy successfully
- [ ] Stop verification: stopped hosts show "Deallocated" state in Portal (not "Stopped")

## 10. Resize Behavior

Azure requires deallocate → modify → start for instance type changes. Troshka attempts hot-resize first (no downtime) and falls back to deallocate+resize if the hot-resize fails.

## Additional Notes

### Terminate Cleanup Order

Azure requires resources to be deleted in dependency order:
1. VM (releases references to NIC and disks)
2. OS disk (separate managed disk)
3. Data disk (separate managed disk)
4. NIC (releases reference to public IP)
5. Public IP

Troshka handles this automatically. Do not delete resources manually via Portal — always use the Troshka API.

### Disk Tagging

Azure does not inherit VM tags to managed disks. Troshka explicitly tags all disks with `managed-by: troshka` and `troshka-host-id` after VM creation.

### Marketplace Terms

If you see errors about marketplace terms not being accepted, run:

```bash
az vm image terms show --publisher redhat --offer rhel-byos --plan rhel-lvm94-gen2
az vm image terms accept --publisher redhat --offer rhel-byos --plan rhel-lvm94-gen2
```

### Network Security Group (NSG) Rules

The NSG created by network setup includes:

- **SSH (22/TCP)**: source `*`, destination `*` (scoped to subnet via NSG association)
- **Agent (31337/TCP)**: source `*`, destination `*`
- **Console (443/TCP)**: source `*`, destination `*`
- **VXLAN (4789/UDP)**: source VirtualNetwork, destination VirtualNetwork (intra-VNet only)

For EIP port forwarding, Troshka creates additional dynamic NSG rules scoped to specific public IPs.

## References

- Azure driver implementation: `src/backend/app/services/providers/azure.py`
- Azure DNS management: `src/backend/app/api/providers.py` (setup-console endpoint)
- Image Builder integration: `src/backend/app/services/image_builder_service.py`
