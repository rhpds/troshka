# AWS Setup Guide

Complete setup instructions for deploying Troshka on Amazon Web Services (AWS).

## Prerequisites

- AWS account with IAM administrative access
- Troshka backend running (see [Common Setup](install-common.md) for backend/frontend/database installation)
- AWS CLI installed (optional, for verification)
- Selected AWS region (default: us-east-1)

## 1. IAM Setup

### Create IAM User

Create a dedicated IAM user for Troshka operations:

1. Sign in to the AWS Console
2. Navigate to IAM → Users → Create User
3. User name: `troshka`
4. Access type: Programmatic access
5. Generate access key pair and save credentials securely

### Create IAM Policy

Create a managed policy `troshka-policy` with the following permission categories:

- **EC2**: Instance lifecycle (run, start, stop, terminate, reboot, modify), instance type discovery, AMI lookup, VPC/subnet/AZ enumeration, key pair management, tagging
- **EBS**: Volume lifecycle (create, delete, attach, detach, modify for resize), volume inspection
- **VPC**: VPC/subnet/Internet Gateway/route table management, VPC attribute modification
- **Security Groups**: Create, delete, describe, authorize/revoke ingress rules
- **Elastic IPs**: Allocate, release, associate, disassociate addresses, assign/unassign private IPs
- **FSx**: File system lifecycle (create, delete, update, describe for FSx OpenZFS), volume management, tagging
- **VPC Endpoints**: Create, delete, describe, modify VPC endpoints (used for S3 Gateway Endpoint)
- **IAM** (one-time): CreateServiceLinkedRole for fsx.amazonaws.com
- **S3**: PutObject, GetObject, DeleteObject, HeadObject, ListBucket on `troshka-images` bucket (used for library uploads, pattern storage, snapshot exports)
- **Route53**: ChangeResourceRecordSets, GetChange, ListHostedZones (console DNS management)
- **IAM** (console): CreateRole, GetRole, PutRolePolicy, DeleteRolePolicy, CreateInstanceProfile, GetInstanceProfile, AddRoleToInstanceProfile, PassRole for `troshka-certbot-role` and `troshka-certbot-profile`

See `infra/iam-policy.json` for the complete policy document.

### Attach Policy

1. Navigate to IAM → Policies → Create Policy
2. JSON tab → paste contents from `infra/iam-policy.json`
3. Name: `troshka-policy`
4. Create policy
5. Navigate to IAM → Users → troshka → Add permissions → Attach policies directly
6. Select `troshka-policy` and attach

## 2. Create Provider

Create an AWS provider in Troshka:

1. Navigate to Admin → Providers
2. Click "Add Provider" button
3. Fill in the form:
   - **Name**: `aws-prod` (or your preferred name)
   - **Type**: Select "EC2"
   - **Access Key ID**: Your AWS access key (AKIA...)
   - **Secret Access Key**: Your AWS secret key
   - **Region**: `us-east-1` (or your preferred region)
4. Click "Create"
5. Click "Test" button to verify credentials
6. Wait for success confirmation message

<details>
<summary>API equivalent</summary>

```bash
curl -X POST http://localhost:8200/api/v1/providers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "aws-prod",
    "type": "ec2",
    "credentials": {
      "access_key_id": "AKIA...",
      "secret_access_key": "...",
      "region": "us-east-1"
    }
  }'
```

The response includes a `provider_id` — save this for subsequent operations.

**Test Credentials:**

```bash
curl -X POST http://localhost:8200/api/v1/providers/{provider_id}/test \
  -H "Authorization: Bearer $TOKEN"
```

Expected response:
```json
{"status": "success", "message": "Provider credentials are valid"}
```

</details>

## 3. VPC Setup

Create a VPC with all required networking infrastructure.

1. Navigate to Admin → Providers
2. Find your provider in the list
3. Click "Setup VPC" button
4. Wait for confirmation message

<details>
<summary>API equivalent</summary>

```bash
curl -X POST http://localhost:8200/api/v1/providers/{provider_id}/create-vpc \
  -H "Authorization: Bearer $TOKEN"
```

</details>

### What Gets Created

- **VPC**: 10.100.0.0/16 CIDR block with DNS support and DNS hostnames enabled
- **Subnets**: One subnet per availability zone (10.100.1.0/24, 10.100.2.0/24, etc.) with auto-assign public IPs enabled
- **Internet Gateway**: Attached to VPC with default route (0.0.0.0/0)
- **Route Table**: Main route table with internet gateway route, associated with all subnets
- **Security Group**: `troshka-host-sg` with the following rules:
  - TCP 22 from 0.0.0.0/0 (SSH access)
  - TCP 443 from 0.0.0.0/0 (Console VNC proxy — HTTPS TLS termination by troshka-vncd)
  - TCP 31337 from backend IP/32 (Troshkad agent API — restricted to backend server)
  - UDP 4789 from same security group (VXLAN mesh for multi-host peering)
- **S3 Gateway Endpoint**: Routes S3 traffic through AWS private network (free, no NAT gateway fees)

All resources are tagged with `ManagedBy: troshka` for identification.

### Verify VPC

The Providers page shows VPC details once setup is complete.

<details>
<summary>API equivalent</summary>

List discovered VPCs:

```bash
curl http://localhost:8200/api/v1/providers/{provider_id}/discover-vpcs \
  -H "Authorization: Bearer $TOKEN"
```

Response includes VPC ID, CIDR block, subnet IDs, and availability zones.

</details>

## 4. S3 Bucket

Create an S3 bucket for library items, pattern storage, and snapshot exports.

1. Navigate to Admin → Providers
2. Find your provider in the list
3. Click "Setup S3" button
4. Wait for confirmation message

This creates a bucket named `troshka-images` in the provider's region. The bucket is required for:
- Library item uploads (ISOs, disk images)
- Pattern storage (captured VM state)
- Snapshot exports (backing up project state)

<details>
<summary>API equivalent</summary>

```bash
curl -X POST http://localhost:8200/api/v1/providers/{provider_id}/create-bucket \
  -H "Authorization: Bearer $TOKEN"
```

</details>

## 5. Host Image

Choose how to obtain the RHEL host image.

### Option A: Marketplace RHEL (Easiest)

Discover and select a RHEL AMI from the marketplace:

1. Navigate to Admin → Providers
2. Find your provider in the list
3. Click "Discover Images" button
4. Wait for the list of available RHEL AMIs to load
5. Click "Set as Default" on a RHEL 9 AMI

<details>
<summary>API equivalent</summary>

Discover available RHEL AMIs in the region:

```bash
curl http://localhost:8200/api/v1/providers/{provider_id}/discover-images \
  -H "Authorization: Bearer $TOKEN"
```

Response includes AMI IDs and descriptions. Select a RHEL 9 AMI and set it as the default:

```bash
curl -X POST http://localhost:8200/api/v1/providers/{provider_id}/set-image \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"image_id": "ami-..."}'
```

</details>

### Option B: Red Hat Image Builder (Recommended for Production)

Build a custom RHEL AMI with all required packages pre-installed. This eliminates:
- RHSM registration at boot time
- Package installation delays during provisioning
- PAYG image premium costs

**Prerequisites:**
1. Obtain a Red Hat offline token from https://access.redhat.com/management/api

**Build Process:**

1. Navigate to Settings → Red Hat Integration
2. Paste offline token into the "Red Hat Offline Token" field
3. Click "Save"
4. Navigate to Admin → Providers
5. Find your provider in the list
6. Click "Build Host Image" button
7. Wait approximately 15 minutes for build completion
8. Image is automatically set as `default_image` when ready

The build progress is shown on the Providers page with a percentage indicator.

<details>
<summary>API equivalent</summary>

```bash
# Start build
curl -X POST http://localhost:8200/api/v1/providers/{provider_id}/build-image \
  -H "Authorization: Bearer $TOKEN"

# Check status
curl http://localhost:8200/api/v1/providers/{provider_id}/image-build-status \
  -H "Authorization: Bearer $TOKEN"
```

Status response includes build state, progress percentage, and image ID when complete. The backend polls Red Hat Image Builder API every 30 seconds and auto-refreshes access tokens.

</details>

## 6. Console Setup

Configure automated DNS management for VNC console access.

### Create Route53 Zone

1. Navigate to Admin → Providers
2. Find your provider in the list
3. Click "Setup Console" button
4. Enter your console domain (e.g., `console.example.com`) in the input field
5. Click "Submit"
6. Wait for confirmation message
7. Note the nameservers displayed in the collapsible section

This creates:
- Route53 hosted zone for the console domain
- IAM role `troshka-certbot-role` with Route53 DNS challenge permissions
- IAM instance profile `troshka-certbot-profile` (attached to hosts for certbot)

<details>
<summary>API equivalent</summary>

```bash
curl -X POST http://localhost:8200/api/v1/providers/{provider_id}/setup-console \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"base_domain": "console.example.com"}'
```

Response includes `console_nameservers` (4 AWS nameservers).

</details>

### Delegate Domain

Add NS records in your parent domain zone pointing to the nameservers returned above. Example for `console.example.com`:

```
console.example.com.  IN  NS  ns-123.awsdns-12.com.
console.example.com.  IN  NS  ns-456.awsdns-23.net.
console.example.com.  IN  NS  ns-789.awsdns-34.org.
console.example.com.  IN  NS  ns-012.awsdns-45.co.uk.
```

### How Console Works

- Each host gets an A record: `{instance_id}.{base_domain}` → host public IP
- Browser connects via WebSocket: `wss://{instance_id}.{base_domain}/ws/{jwt}`
- Backend issues short-lived JWT (5 min, single-use) signed with host agent token
- `troshka-vncd` daemon on host validates JWT, resolves VNC port, proxies frames
- TLS via Let's Encrypt (certbot DNS-01 challenge using Route53 instance profile)
- Auto-renewal via cron: `certbot renew --quiet`

## 7. Provision First Host

Provision a host for running nested VMs.

1. Navigate to Admin → Providers
2. Find your provider in the list
3. Scroll down to the "Hosts" section
4. Click "Add Host" button
5. Fill in the form:
   - **Instance Type**: Select from dropdown (e.g., `m5.metal` for production, `m8i.xlarge` for dev)
   - **Storage Size (GB)**: Enter desired size (minimum 100, recommended 500 for production)
6. Click "Create"
7. Wait 3-5 minutes for provisioning to complete
8. Host status will change from "provisioning" to "connected" when ready

<details>
<summary>API equivalent</summary>

```bash
curl -X POST http://localhost:8200/api/v1/hosts \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "provider_id": "...",
    "instance_type": "m5.metal",
    "storage_size_gb": 500
  }'
```

Provisioning takes 3-5 minutes. The API returns immediately with `status: "provisioning"`. Check host status:

```bash
curl http://localhost:8200/api/v1/hosts \
  -H "Authorization: Bearer $TOKEN"
```

When `status: "connected"`, the host is ready for deployments.

</details>

### Recommended Instance Types

| Instance Type | vCPUs | RAM | Use Case | Notes |
|--------------|-------|-----|----------|-------|
| m5.metal | 96 | 384 GB | Production | Full bare-metal, best nested virt performance |
| c5.metal | 96 | 192 GB | Production | Compute-heavy labs, lower cost than m5.metal |
| m8i.xlarge | 4 | 16 GB | Dev/Testing | Nested virt enabled, cost-effective for testing |
| m8i.2xlarge | 8 | 32 GB | Dev/Testing | More capacity than xlarge, still affordable |

### Storage Sizing

- **Development**: 100 GB minimum (sufficient for testing workflows)
- **Production**: 500 GB or more (depends on VM disk sizes and pattern caching)
- Storage is EBS gp3 (provisioned automatically, online resizable via admin UI)

## 8. Shared Storage (Optional)

Create an FSx OpenZFS storage pool for live migration and pattern sharing across hosts.

### Create Storage Pool

1. Navigate to Admin → Storage Pools
2. Click "Create Storage Pool" button
3. Fill in the form:
   - **Name**: `shared-pool` (or your preferred name)
   - **Mode**: Select "FSx OpenZFS"
   - **Provider**: Select your provider from dropdown
   - **Availability Zone**: Select an AZ (e.g., `us-east-1a`)
4. Click "Create"
5. Wait approximately 5 minutes for FSx file system creation

**Important:** All hosts in the pool must be in the same AZ. FSx OpenZFS is single-AZ.

<details>
<summary>API equivalent</summary>

```bash
curl -X POST http://localhost:8200/api/v1/storage-pools \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "shared-pool",
    "mode": "shared-fsx",
    "provider_id": "...",
    "availability_zone": "us-east-1a"
  }'
```

</details>

### What Gets Created

- FSx OpenZFS file system (~5 min creation time)
- NFS security group rules (TCP 2049)
- Libvirt TLS security group rules (TCP 16514 for control, TCP 49152-49215 for live migration data)

### Configuration

- **Mount options**: `nconnect=16`, `cache=none,io=native`, LZ4 compression enabled
- **Pricing**: Per-second billing, no minimum commitment (~$53/month for 128 GB storage / 160 MBps throughput)
- **Auto-extend**: Configurable threshold (default 80%) and increment (default 128 GB)

### Benefits

- Live migration between hosts in the pool
- Shared pattern cache (one download serves all hosts)
- Automatic failover (move projects off failed host to healthy hosts)

## 9. Verify Installation

Run through this checklist to confirm the setup is working:

### Provider Test

1. Navigate to Admin → Providers
2. Find your provider in the list
3. Click "Test" button
4. Verify success message appears

<details>
<summary>API equivalent</summary>

```bash
curl -X POST http://localhost:8200/api/v1/providers/{provider_id}/test \
  -H "Authorization: Bearer $TOKEN"
```
Expected: `{"status": "success"}`

</details>

### VPC Discovery

Verify VPC details are shown on the Providers page with `ManagedBy: troshka` tag.

<details>
<summary>API equivalent</summary>

```bash
curl http://localhost:8200/api/v1/providers/{provider_id}/discover-vpcs \
  -H "Authorization: Bearer $TOKEN"
```
Expected: Response includes VPC with `ManagedBy: troshka` tag

</details>

### Host Status

1. Navigate to Admin → Providers
2. Scroll down to the "Hosts" section
3. Verify host shows `status: "connected"` with instance ID, public IP, and console domain

<details>
<summary>API equivalent</summary>

```bash
curl http://localhost:8200/api/v1/hosts \
  -H "Authorization: Bearer $TOKEN"
```
Expected: Host shows `status: "connected"`, instance ID, public IP, console domain

</details>

### Agent Version

Check that the host's agent version matches the expected version shown on the Providers page. If mismatched, run:
```bash
./scripts/update-agent.sh
```

<details>
<summary>API equivalent</summary>

```bash
curl http://localhost:8200/api/v1/hosts/expected-agent-version \
  -H "Authorization: Bearer $TOKEN"
```
Compare response with host's `agent_version` field.

</details>

### Console Access

After deploying a VM with a project:

1. Navigate to the project's Hosts page
2. Click the console icon for a running VM
3. Browser should open `https://{instance_id}.{base_domain}` with noVNC console
4. Verify keyboard input and mouse work correctly

### Deploy Test

Create a minimal project and deploy:

1. Navigate to Projects → Create Project
2. Add a VM node to the canvas
3. Deploy the project
4. Verify VM starts successfully (check project Hosts page for VM status)
5. Open console and verify VM is running

## Troubleshooting

### Host Stuck in Provisioning
- Check AWS Console EC2 instances for the host
- Verify instance launched successfully
- Check backend logs for provisioning errors
- Verify security group rules allow port 31337 from backend IP

### Console Not Loading
- Verify DNS delegation (dig `{instance_id}.{base_domain}` should return host IP)
- Check certbot status on host via `scripts/host-ssh.sh -- 'sudo systemctl status certbot.timer'`
- Verify troshka-vncd is running: `scripts/host-ssh.sh -- 'sudo systemctl status troshka-vncd'`
- Check browser console for WebSocket errors

### FSx Pool Creation Failed
- Verify IAM policy includes FSx permissions
- Check one-time service-linked role exists: `aws iam get-role --role-name AWSServiceRoleForFSx`
- If missing, create it: `aws iam create-service-linked-role --aws-service-name fsx.amazonaws.com`
- Verify AZ supports FSx OpenZFS: check AWS FSx documentation for regional availability

### Pattern Save Fails
- Verify S3 bucket exists and credentials are correct
- Check host has internet access (test with `scripts/host-ssh.sh -- 'curl -I https://s3.amazonaws.com'`)
- Verify S3 Gateway Endpoint is attached to VPC route table
- Check backend logs for S3 API errors

## Next Steps

- [Common Setup](install-common.md) — Backend/frontend/database installation (if not done)
- [API Guide](api-guide.md) — Workflow walkthroughs and full endpoint reference
- [Architecture Guide](architecture.md) — System design, provider abstraction, agent internals
