# Troshka API Guide

Comprehensive guide to the Troshka REST API. For architecture and concepts, see the [Architecture Guide](architecture.md).

## Table of Contents

### Part 1 — Workflow Walkthroughs

1. [Getting Started](#section-1-getting-started)
2. [Setting Up Infrastructure (admin)](#section-2-setting-up-infrastructure-admin)
3. [Managing Your Library](#section-3-managing-your-library)
4. [Building & Deploying Projects](#section-4-building--deploying-projects)
5. [Working with VMs](#section-5-working-with-vms)
6. [Patterns](#section-6-patterns)
7. [Networking & External Access](#section-7-networking--external-access)
8. [Administration](#section-8-administration)
9. [Portal Access](#section-9-portal-access)

### Part 2 — Full Endpoint Reference

[See below](#part-2--full-endpoint-reference)

---

## Part 1 — Workflow Walkthroughs

### Section 1: Getting Started

#### Base URL

All API endpoints are prefixed with:

```
http://localhost:8200/api/v1
```

In production, use your actual domain.

#### Authentication

Troshka supports three authentication methods:

**1. Dev Token (development only)**

Get a JWT token for local development:

```bash
curl http://localhost:8200/api/v1/auth/dev-token
```

Response:

```json
{
  "token": "YOUR_DEV_TOKEN",
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "email": "local-dev@troshka",
  "display_name": "local-dev",
  "role": "admin"
}
```

Use the token in subsequent requests:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8200/api/v1/projects
```

You can also get a token with a specific role:

```bash
curl http://localhost:8200/api/v1/auth/dev-token/user
curl http://localhost:8200/api/v1/auth/dev-token/operator
curl http://localhost:8200/api/v1/auth/dev-token/admin
```

**2. API Keys (production)**

Create an API key with a name and optional expiration:

```bash
curl -X POST http://localhost:8200/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "ci-pipeline",
    "expires_in_days": 90
  }'
```

Response:

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440001",
  "name": "ci-pipeline",
  "key": "trk_YOUR_API_KEY",
  "expires_at": "2026-09-14T12:00:00Z"
}
```

Use the API key as a bearer token:

```bash
curl -H "Authorization: Bearer trk_YOUR_API_KEY" \
  http://localhost:8200/api/v1/projects
```

API keys are only shown once at creation. Store them securely.

**3. OIDC/SSO (production)**

When `oauth_enabled` is true in config, Troshka expects OAuth proxy headers:
- `X-Forwarded-Email`: user's email
- `X-Forwarded-User`: user's display name

#### Common Patterns

**Async Operations**

Long-running operations (deploy, provision, pattern capture) return HTTP 202 Accepted immediately. Poll the progress endpoint or subscribe via WebSocket.

Example deploy flow:

```bash
# Start deployment (returns 202)
curl -X POST http://localhost:8200/api/v1/projects/{id}/deploy \
  -H "Authorization: Bearer $TOKEN"

# Poll progress
curl http://localhost:8200/api/v1/projects/{id}/deploy-progress \
  -H "Authorization: Bearer $TOKEN"
```

Response:

```json
{
  "total": 100,
  "completed": 45,
  "status": "deploying",
  "detail": "Creating VM disks (2/5)",
  "active_operations": [
    {
      "operation": "download",
      "filename": "rhel-9.5.qcow2",
      "bytes_downloaded": 1073741824,
      "total_bytes": 2147483648,
      "speed_mbps": 125.5
    }
  ]
}
```

**WebSocket Subscriptions**

Subscribe to real-time updates for projects or patterns:

```javascript
const ws = new WebSocket('ws://localhost:8200/api/v1/projects/{id}/ws');
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  if (msg.type === 'vm_state') {
    console.log(`VM ${msg.vm_id} is now ${msg.state}`);
  }
};
```

Pattern capture progress:

```javascript
const ws = new WebSocket('ws://localhost:8200/api/v1/patterns/{id}/ws');
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  console.log(`Progress: ${msg.completed}/${msg.total}`);
};
```

**Standard Error Format**

All errors return a JSON object with a `detail` field:

```json
{
  "detail": "Project not found"
}
```

HTTP status codes follow REST conventions:
- 200: Success
- 201: Created
- 202: Accepted (async operation started)
- 204: No Content (successful delete)
- 400: Bad Request (validation error)
- 401: Unauthorized (missing/invalid auth)
- 403: Forbidden (insufficient permissions)
- 404: Not Found
- 409: Conflict (duplicate name, etc.)
- 500: Internal Server Error

---

### Section 2: Setting Up Infrastructure (admin)

This section covers initial setup: providers, networking, hosts, and storage pools. Requires `admin` role.

#### Create a Provider

Providers represent cloud accounts. Each cloud type requires different credentials.

**AWS (EC2)**

```bash
curl -X POST http://localhost:8200/api/v1/providers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "AWS Production",
    "type": "ec2",
    "access_key_id": "YOUR_ACCESS_KEY_ID",
    "secret_access_key": "YOUR_SECRET_ACCESS_KEY",
    "default_region": "us-east-1",
    "bucket": "troshka-images"
  }'
```

**GCP (Compute Engine)**

```bash
curl -X POST http://localhost:8200/api/v1/providers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "GCP Development",
    "type": "gcp",
    "gcp_project_id": "my-project-123456",
    "service_account_json": "{\"type\":\"service_account\",\"project_id\":\"my-project\",...}"
  }'
```

**Azure**

```bash
curl -X POST http://localhost:8200/api/v1/providers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Azure Staging",
    "type": "azure",
    "azure_tenant_id": "00000000-0000-0000-0000-000000000000",
    "azure_client_id": "11111111-1111-1111-1111-111111111111",
    "azure_client_secret": "my-client-secret",
    "azure_subscription_id": "22222222-2222-2222-2222-222222222222",
    "azure_location": "eastus"
  }'
```

**OpenShift Virtualization**

```bash
curl -X POST http://localhost:8200/api/v1/providers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "OCP Virt Dev",
    "type": "ocpvirt",
    "api_url": "https://api.cluster.example.com:6443",
    "token": "sha256~abc123...",
    "namespace": "troshka",
    "verify_ssl": false
  }'
```

Response (all provider types):

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "AWS Production",
  "type": "ec2",
  "default_region": "us-east-1",
  "state": "active",
  "has_credentials": true,
  "host_count": 0,
  "created_at": "2026-06-16T10:00:00Z"
}
```

#### Test Provider Credentials

```bash
curl -X POST http://localhost:8200/api/v1/providers/{id}/test \
  -H "Authorization: Bearer $TOKEN"
```

Response:

```json
{
  "success": true,
  "message": "Provider credentials are valid"
}
```

#### Network Setup

Network setup varies per cloud provider.

**AWS: Create VPC**

```bash
curl -X POST http://localhost:8200/api/v1/providers/{id}/create-vpc \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "region": "us-east-1"
  }'
```

Response:

```json
{
  "vpc_id": "vpc-abc123",
  "subnet_id": "subnet-def456",
  "security_group_id": "sg-ghi789"
}
```

**GCP: Create Network**

```bash
curl -X POST http://localhost:8200/api/v1/providers/{id}/create-network-gcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "zone": "us-central1-a"
  }'
```

**Azure: Create Network**

```bash
curl -X POST http://localhost:8200/api/v1/providers/{id}/create-network-azure \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "resource_group": "troshka-rg",
    "location": "eastus"
  }'
```

**OCP Virt: Setup Infrastructure**

```bash
curl -X POST http://localhost:8200/api/v1/providers/{id}/setup-infra \
  -H "Authorization: Bearer $TOKEN"
```

#### Console Setup (Optional)

Enable VNC console access with TLS via Route53 (AWS) or Cloud DNS (GCP/Azure):

```bash
curl -X POST http://localhost:8200/api/v1/providers/{id}/setup-console \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "base_domain": "console.example.com"
  }'
```

Response:

```json
{
  "zone_id": "Z1234567890ABC",
  "base_domain": "console.example.com",
  "nameservers": [
    "ns-123.awsdns-12.com",
    "ns-456.awsdns-45.net",
    "ns-789.awsdns-78.org",
    "ns-012.awsdns-01.co.uk"
  ]
}
```

Add NS records in your parent zone pointing to these nameservers.

#### S3 Bucket Setup (AWS only)

```bash
curl -X POST http://localhost:8200/api/v1/providers/{id}/create-bucket \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "bucket": "troshka-images-prod"
  }'
```

#### Set Default Host Image

```bash
curl -X POST http://localhost:8200/api/v1/providers/{id}/set-image \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "image_id": "ami-0abcdef1234567890"
  }'
```

For GCP and Azure, use the image URN or managed image resource ID.

#### Provision a Host

```bash
curl -X POST http://localhost:8200/api/v1/hosts \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "provider_id": "550e8400-e29b-41d4-a716-446655440000",
    "instance_type": "c5.metal",
    "storage_pool_id": null
  }'
```

Response (202 Accepted):

```json
{
  "id": "660e8400-e29b-41d4-a716-446655440001",
  "provider_id": "550e8400-e29b-41d4-a716-446655440000",
  "instance_id": "i-0123456789abcdef0",
  "instance_type": "c5.metal",
  "state": "provisioning",
  "agent_status": "installing"
}
```

The host provisions asynchronously. Check status:

```bash
curl http://localhost:8200/api/v1/hosts \
  -H "Authorization: Bearer $TOKEN"
```

When `state` is `active` and `agent_status` is `connected`, the host is ready.

---

### Section 3: Managing Your Library

The library stores ISOs and disk images in S3. Each user has a personal library, plus access to shared items.

#### List Library Items

```bash
curl http://localhost:8200/api/v1/library \
  -H "Authorization: Bearer $TOKEN"
```

Query parameters:
- `type`: Filter by type (`image` or `iso`)
- `q`: Search by name

Response:

```json
[
  {
    "id": "770e8400-e29b-41d4-a716-446655440002",
    "name": "RHEL 9.5 Server",
    "description": "Red Hat Enterprise Linux 9.5",
    "type": "image",
    "format": "qcow2",
    "size_bytes": 2147483648,
    "os_variant": "rhel9.5",
    "state": "available",
    "tags": null,
    "created_at": "2026-06-15T14:30:00Z",
    "owned": true,
    "owner_id": "550e8400-e29b-41d4-a716-446655440000"
  }
]
```

#### Upload an Image (Multipart)

For large files, use multipart upload:

**Step 1: Create library entry**

```bash
curl -X POST http://localhost:8200/api/v1/library \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Ubuntu 24.04 Server",
    "description": "Ubuntu 24.04 LTS",
    "type": "image",
    "format": "qcow2",
    "os_variant": "ubuntu24.04"
  }'
```

Response:

```json
{
  "id": "880e8400-e29b-41d4-a716-446655440003",
  "name": "Ubuntu 24.04 Server",
  "state": "pending"
}
```

**Step 2: Start multipart upload**

```bash
curl -X POST http://localhost:8200/api/v1/library/{id}/upload-start \
  -H "Authorization: Bearer $TOKEN"
```

Response:

```json
{
  "upload_id": "abc123def456"
}
```

**Step 3: Upload parts**

For each part (5MB - 5GB):

```bash
curl -X POST http://localhost:8200/api/v1/library/{id}/upload-part-url \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "upload_id": "abc123def456",
    "part_number": 1
  }'
```

Response:

```json
{
  "url": "https://s3.amazonaws.com/...",
  "part_number": 1
}
```

PUT the file part to the presigned URL (not via Troshka API):

```bash
curl -X PUT "https://s3.amazonaws.com/..." \
  --upload-file part1.bin
```

Repeat for each part.

**Step 4: Complete upload**

```bash
curl -X POST http://localhost:8200/api/v1/library/{id}/upload-complete \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "upload_id": "abc123def456",
    "parts": [
      {"part_number": 1, "etag": "abc123..."},
      {"part_number": 2, "etag": "def456..."}
    ]
  }'
```

#### Import from URL

For public ISOs/images:

```bash
curl -X POST http://localhost:8200/api/v1/library/{id}/import-url \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://download.fedoraproject.org/pub/fedora/linux/releases/40/Server/x86_64/iso/Fedora-Server-dvd-x86_64-40-1.14.iso"
  }'
```

Returns 202 Accepted. Poll item state:

```bash
curl http://localhost:8200/api/v1/library/{id} \
  -H "Authorization: Bearer $TOKEN"
```

When `state` is `available`, the import is complete.

#### Share a Library Item

```bash
curl -X POST http://localhost:8200/api/v1/library/{id}/share \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "colleague@example.com"
  }'
```

The recipient will see the item in their library list (with `owned: false`).

---

### Section 4: Building & Deploying Projects

Projects are the core abstraction: a topology of VMs, networks, and storage deployed to a host.

#### Create a Project

```bash
curl -X POST http://localhost:8200/api/v1/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Lab",
    "description": "Test environment"
  }'
```

Response:

```json
{
  "id": "990e8400-e29b-41d4-a716-446655440004",
  "name": "My Lab",
  "description": "Test environment",
  "owner_id": "550e8400-e29b-41d4-a716-446655440000",
  "state": "draft",
  "topology": null,
  "created_at": "2026-06-16T12:00:00Z"
}
```

#### Update Project Topology

The topology is a JSONB structure defining VMs, networks, and storage. Example minimal topology:

```bash
curl -X PATCH http://localhost:8200/api/v1/projects/{id} \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "topology": {
      "nodes": [
        {
          "id": "aa0e8400-e29b-41d4-a716-446655440005",
          "type": "vmNode",
          "position": {"x": 100, "y": 100},
          "data": {
            "name": "server1",
            "vcpus": 4,
            "ram": 8,
            "os": "rhel9",
            "nics": [
              {
                "id": "nic-bb0e8400",
                "mac": "52:54:00:12:34:56",
                "model": "virtio"
              }
            ],
            "diskControllers": [],
            "bootDevices": ["network"]
          }
        },
        {
          "id": "cc0e8400-e29b-41d4-a716-446655440006",
          "type": "networkNode",
          "position": {"x": 300, "y": 100},
          "data": {
            "name": "net1",
            "networkType": "isolated",
            "cidr": "192.168.100.0/24",
            "dhcp": {
              "enabled": true,
              "start": "192.168.100.10",
              "end": "192.168.100.200"
            }
          }
        }
      ],
      "edges": [
        {
          "id": "xy-edge__aa0e8400nic-bb0e8400-cc0e8400",
          "source": "aa0e8400-e29b-41d4-a716-446655440005",
          "target": "cc0e8400-e29b-41d4-a716-446655440006",
          "sourceHandle": "nic-bb0e8400",
          "targetHandle": null
        }
      ]
    }
  }'
```

Topology nodes:
- `vmNode`: Virtual machine (data: name, vcpus, ram, os, nics, diskControllers, bootDevices)
- `containerNode`: Container or pod (data: image, command, ports, env; pods have isPod=true, initContainers, podContainers)
- `networkNode`: Network (data: networkType, cidr, dhcp)
- `storageNode`: Disk/ISO (data: format, size, libraryItemId)

#### Deploy a Project

```bash
curl -X POST http://localhost:8200/api/v1/projects/{id}/deploy \
  -H "Authorization: Bearer $TOKEN"
```

Returns 202 Accepted. The backend selects a host, provisions networks, downloads images, creates VMs, and starts them.

**Monitor Progress**

```bash
curl http://localhost:8200/api/v1/projects/{id}/deploy-progress \
  -H "Authorization: Bearer $TOKEN"
```

Response:

```json
{
  "total": 100,
  "completed": 67,
  "status": "deploying",
  "detail": "Starting VMs (2/3)",
  "active_operations": [
    {
      "operation": "download",
      "filename": "rhel-9.5.qcow2",
      "bytes_downloaded": 1610612736,
      "total_bytes": 2147483648,
      "speed_mbps": 150.2
    }
  ]
}
```

When `completed` equals `total` and `status` is `deployed`, the project is running.

#### Create from Template

Pre-defined topologies (OCP IPI, OCP Agent, etc.):

```bash
curl -X POST http://localhost:8200/api/v1/projects/from-template \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "template_id": "ocp-ipi-3-node",
    "name": "OCP 4.20 Lab",
    "cluster_name": "ocp",
    "base_domain": "ocp.local",
    "ocp_version": "4.20",
    "bastion_password": "redhat123",
    "bastion_image_id": "880e8400-e29b-41d4-a716-446655440003",
    "bastion_ssh_key_id": 1,
    "auto_install_ocp": true
  }'
```

Response:

```json
{
  "id": "dd0e8400-e29b-41d4-a716-446655440007",
  "name": "OCP 4.20 Lab"
}
```

---

### Section 5: Working with VMs

Once a project is deployed, you can control VMs, access consoles, and transfer files.

#### Power Controls

Start a VM:

```bash
curl -X POST http://localhost:8200/api/v1/projects/{id}/vms/{vm_id}/start \
  -H "Authorization: Bearer $TOKEN"
```

Stop (graceful):

```bash
curl -X POST http://localhost:8200/api/v1/projects/{id}/vms/{vm_id}/stop \
  -H "Authorization: Bearer $TOKEN"
```

Force stop (immediate):

```bash
curl -X POST http://localhost:8200/api/v1/projects/{id}/vms/{vm_id}/forcestop \
  -H "Authorization: Bearer $TOKEN"
```

Restart:

```bash
curl -X POST http://localhost:8200/api/v1/projects/{id}/vms/{vm_id}/restart \
  -H "Authorization: Bearer $TOKEN"
```

#### Batch VM States

Get all VM states in one call:

```bash
curl http://localhost:8200/api/v1/projects/{id}/vm-states \
  -H "Authorization: Bearer $TOKEN"
```

Response:

```json
{
  "aa0e8400-e29b-41d4-a716-446655440005": {
    "state": "running",
    "vcpus": 4,
    "ram_mb": 8192
  },
  "ee0e8400-e29b-41d4-a716-446655440008": {
    "state": "stopped"
  }
}
```

#### Console Access

Get a WebSocket URL for VNC console:

```bash
curl http://localhost:8200/api/v1/projects/{id}/vms/{vm_id}/console \
  -H "Authorization: Bearer $TOKEN"
```

Response:

```json
{
  "url": "wss://abc123.console.example.com/ws/JWT_TOKEN",
  "token": "JWT_TOKEN"
}
```

Connect via noVNC client to the `url`.

#### Execute Commands

Run a command via serial console or SSH:

```bash
curl -X POST http://localhost:8200/api/v1/projects/{id}/vms/{vm_id}/exec \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "command": "uptime",
    "method": "serial",
    "timeout": 30
  }'
```

Methods:
- `serial`: Serial console (always works, no network required)
- `ssh`: SSH (requires network and credentials)

Response:

```json
{
  "stdout": " 12:34:56 up 1 day,  2:15,  0 users,  load average: 0.00, 0.01, 0.05\n",
  "stderr": "",
  "exit_code": 0
}
```

#### File Transfer

**Push a file to VM:**

```bash
curl -X PUT http://localhost:8200/api/v1/projects/{id}/vms/{vm_id}/files \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "path": "/tmp/config.yaml",
    "content": "key: value\n",
    "mode": "0644"
  }'
```

**Pull a file from VM:**

```bash
curl "http://localhost:8200/api/v1/projects/{id}/vms/{vm_id}/files?path=/etc/hosts" \
  -H "Authorization: Bearer $TOKEN"
```

Response:

```json
{
  "path": "/etc/hosts",
  "content": "127.0.0.1   localhost\n...",
  "mode": "0644"
}
```

#### Snapshots

Create a snapshot:

```bash
curl -X POST http://localhost:8200/api/v1/projects/{id}/vms/{vm_id}/snapshot \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Pre-upgrade backup"
  }'
```

Returns 202 Accepted. The snapshot is saved to S3 and added to your library.

---

### Section 6: Patterns

Patterns are reusable VM topologies captured from running projects. They preserve the entire state: disk images, network config, BMC credentials, etc.

#### Save a Pattern

```bash
curl -X POST http://localhost:8200/api/v1/patterns \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "OCP 4.20 Single Node",
    "description": "Fully configured OCP SNO cluster",
    "project_id": "dd0e8400-e29b-41d4-a716-446655440007",
    "visibility": "private"
  }'
```

Response:

```json
{
  "id": "ff0e8400-e29b-41d4-a716-446655440009",
  "name": "OCP 4.20 Single Node",
  "state": "creating"
}
```

The pattern creation runs asynchronously. Stages:
1. `creating`: Setting up S3 prefix
2. `capturing`: Uploading disk images (can take 10+ minutes)
3. `available`: Ready to deploy

**Monitor Progress**

```bash
curl http://localhost:8200/api/v1/patterns/{id}/progress \
  -H "Authorization: Bearer $TOKEN"
```

Response:

```json
{
  "total": 100,
  "completed": 58,
  "status": "capturing",
  "detail": "Uploading disk 2 of 3",
  "active_operations": [
    {
      "operation": "upload",
      "filename": "server1-disk1.qcow2",
      "bytes_uploaded": 3221225472,
      "total_bytes": 5368709120,
      "speed_mbps": 200.5
    }
  ]
}
```

Or use WebSocket:

```javascript
const ws = new WebSocket('ws://localhost:8200/api/v1/patterns/{id}/ws');
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  console.log(`Progress: ${msg.completed}/${msg.total} - ${msg.detail}`);
};
```

#### Deploy a Pattern

```bash
curl -X POST http://localhost:8200/api/v1/patterns/{id}/deploy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "project_name": "Lab Instance 1"
  }'
```

Response:

```json
{
  "project_id": "000e8400-e29b-41d4-a716-446655440010",
  "project_name": "Lab Instance 1"
}
```

The project is created and deployed automatically. Monitor via `/projects/{project_id}/deploy-progress`.

#### Bulk Deploy

Deploy multiple copies at once:

```bash
curl -X POST http://localhost:8200/api/v1/patterns/{id}/bulk-deploy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "count": 10,
    "name_template": "student-lab-{n}"
  }'
```

Creates 10 projects: `student-lab-1`, `student-lab-2`, ... `student-lab-10`.

Response:

```json
{
  "projects": [
    {
      "project_id": "111e8400-e29b-41d4-a716-446655440011",
      "project_name": "student-lab-1"
    },
    {
      "project_id": "222e8400-e29b-41d4-a716-446655440012",
      "project_name": "student-lab-2"
    }
  ]
}
```

#### Share a Pattern

```bash
curl -X POST http://localhost:8200/api/v1/patterns/{id}/share \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "instructor@example.com"
  }'
```

The recipient can deploy the pattern but not modify it.

---

### Section 7: Networking & External Access

#### Network CRUD

List networks in a project:

```bash
curl http://localhost:8200/api/v1/projects/{id}/networks \
  -H "Authorization: Bearer $TOKEN"
```

Create a network:

```bash
curl -X POST http://localhost:8200/api/v1/projects/{id}/networks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "dmz",
    "networkType": "isolated",
    "cidr": "10.1.2.0/24",
    "dhcp": {
      "enabled": true,
      "start": "10.1.2.10",
      "end": "10.1.2.200"
    }
  }'
```

Update a network:

```bash
curl -X PATCH http://localhost:8200/api/v1/projects/{id}/networks/{network_id} \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "dhcp": {
      "enabled": false
    }
  }'
```

Delete a network:

```bash
curl -X DELETE http://localhost:8200/api/v1/projects/{id}/networks/{network_id} \
  -H "Authorization: Bearer $TOKEN"
```

#### External IPs

List EIPs allocated to a project:

```bash
curl http://localhost:8200/api/v1/projects/{id}/eips \
  -H "Authorization: Bearer $TOKEN"
```

Response:

```json
[
  {
    "id": "eip-333e8400",
    "name": "bastion-public",
    "ip": "54.123.45.67",
    "vm_id": "aa0e8400-e29b-41d4-a716-446655440005",
    "port_forwards": [
      {"internal_port": 22, "external_port": 22, "protocol": "tcp"}
    ]
  }
]
```

Delete an EIP:

```bash
curl -X DELETE http://localhost:8200/api/v1/projects/{id}/eips/{canvas_eip_id} \
  -H "Authorization: Bearer $TOKEN"
```

#### DNS Providers

Create a DNS provider (Route53 example):

```bash
curl -X POST http://localhost:8200/api/v1/dns-providers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Route53 Production",
    "type": "route53",
    "config": {
      "access_key_id": "YOUR_ACCESS_KEY_ID",
      "secret_access_key": "YOUR_SECRET_ACCESS_KEY",
      "region": "us-east-1"
    }
  }'
```

List DNS providers:

```bash
curl http://localhost:8200/api/v1/dns-providers \
  -H "Authorization: Bearer $TOKEN"
```

---

### Section 8: Administration

Admin-only endpoints for host lifecycle, storage pools, and provider management.

#### Host Lifecycle

**Resize a Host**

```bash
curl -X POST http://localhost:8200/api/v1/hosts/{id}/resize \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "instance_type": "c5.metal",
    "storage_size_gb": 1000
  }'
```

Requires stopping all projects on the host first. AWS/GCP/Azure only (not OCP Virt).

**Extend Host Storage**

```bash
curl -X POST http://localhost:8200/api/v1/hosts/{id}/extend-storage \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "new_size_gb": 1500
  }'
```

Returns 202 Accepted. AWS EBS volumes support online resize. Poll host state for completion.

**Update Agent**

Push latest troshkad version to host:

```bash
curl -X POST http://localhost:8200/api/v1/hosts/{id}/update-agent \
  -H "Authorization: Bearer $TOKEN"
```

**Evacuate Host**

Move all projects to other hosts in the same storage pool:

```bash
curl -X POST http://localhost:8200/api/v1/hosts/{id}/evacuate \
  -H "Authorization: Bearer $TOKEN"
```

Returns 202 Accepted. Migration happens asynchronously.

**Wipe Host**

Remove all projects and networks, clean up disk space:

```bash
curl -X POST http://localhost:8200/api/v1/hosts/{id}/wipe \
  -H "Authorization: Bearer $TOKEN"
```

Returns 202 Accepted. Does NOT delete the host instance. Cache directories (`/var/lib/troshka/images/`, `/var/lib/troshka/cache/`) are preserved.

**Run Garbage Collector**

```bash
curl -X POST http://localhost:8200/api/v1/hosts/{id}/gc \
  -H "Authorization: Bearer $TOKEN"
```

Steps: capacity sync, orphan cleanup, network repair, cache eviction, S3 cleanup. Returns detailed report of cleaned items.

**Terminate Host**

Permanently destroy the host instance:

```bash
curl -X DELETE http://localhost:8200/api/v1/hosts/{id} \
  -H "Authorization: Bearer $TOKEN"
```

Wipes all data and terminates the EC2/GCE/Azure VM.

#### Storage Pools

**Create a Pool**

FSx OpenZFS (AWS):

```bash
curl -X POST http://localhost:8200/api/v1/storage-pools \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "shared-pool-1",
    "mode": "shared-fsx",
    "provider_id": "550e8400-e29b-41d4-a716-446655440000",
    "az": "us-east-1a",
    "fsx_throughput_mbps": 256,
    "fsx_storage_gb": 256
  }'
```

BYO NFS:

```bash
curl -X POST http://localhost:8200/api/v1/storage-pools \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "nfs-pool",
    "mode": "shared-byo",
    "nfs_endpoint": "10.0.1.100:/exports/troshka"
  }'
```

**Extend Pool Storage**

```bash
curl -X POST http://localhost:8200/api/v1/storage-pools/{id}/extend \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "increment_gb": 128
  }'
```

Returns 202 Accepted. FSx has a 6-hour cooldown between extends.

**Pattern Buffer**

Provision a dedicated worker host for pattern captures:

```bash
curl -X POST http://localhost:8200/api/v1/storage-pools/{id}/provision-pattern-buffer \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "instance_type": "i4i.2xlarge"
  }'
```

Returns 202 Accepted. The worker uses local NVMe for fast captures.

**Cache Management**

List shared cache entries:

```bash
curl http://localhost:8200/api/v1/storage-pools/{id}/cache \
  -H "Authorization: Bearer $TOKEN"
```

Evict a cache entry:

```bash
curl -X DELETE http://localhost:8200/api/v1/storage-pools/{id}/cache/{entry_id} \
  -H "Authorization: Bearer $TOKEN"
```

#### Provider Management

**Build Host Image (Red Hat Image Builder)**

Build a custom RHEL image with all packages pre-installed:

```bash
curl -X POST http://localhost:8200/api/v1/providers/{id}/build-image \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "target": "gcp"
  }'
```

Targets: `aws`, `gcp`, `azure`. Returns 202 Accepted. Poll status:

```bash
curl http://localhost:8200/api/v1/providers/{id}/image-build-status \
  -H "Authorization: Bearer $TOKEN"
```

Response:

```json
{
  "status": "building",
  "progress": 45,
  "detail": "Building image (stage 3/5)",
  "image_id": null
}
```

When status is `complete`, the `image_id` is set and automatically configured as the provider's `default_image`.

---

### Section 9: Portal Access

Portal tokens allow unauthenticated users to view and control specific projects.

#### Generate Portal Token

```bash
curl -X POST http://localhost:8200/api/v1/projects/{id}/portal-token \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "access_level": "power"
  }'
```

Access levels:
- `readonly`: View topology and VM states only
- `power`: View + start/stop VMs
- `console`: View + power + console access
- `manage`: Full control (edit topology, deploy, destroy)

Response:

```json
{
  "token": "pt_PORTAL_TOKEN",
  "access_level": "power",
  "portal_url": "http://localhost:8200/portal/pt_PORTAL_TOKEN"
}
```

#### View Project via Portal

No authentication required (token is the auth):

```bash
curl http://localhost:8200/api/v1/portal/pt_PORTAL_TOKEN
```

Response:

```json
{
  "project_id": "990e8400-e29b-41d4-a716-446655440004",
  "project_name": "My Lab",
  "project_state": "deployed",
  "access_level": "power",
  "topology": {
    "nodes": [...],
    "edges": [...]
  }
}
```

Hidden nodes (from `topology.hiddenNodeIds`) are filtered out.

#### Get VM States

```bash
curl http://localhost:8200/api/v1/portal/pt_PORTAL_TOKEN/vm-states
```

Response:

```json
{
  "states": {
    "aa0e8400-e29b-41d4-a716-446655440005": "running",
    "ee0e8400-e29b-41d4-a716-446655440008": "stopped"
  }
}
```

#### VM Actions via Portal

Start/stop/restart a VM (requires `power` level or higher):

```bash
curl -X POST http://localhost:8200/api/v1/portal/pt_PORTAL_TOKEN/vms/{vm_id}/start
curl -X POST http://localhost:8200/api/v1/portal/pt_PORTAL_TOKEN/vms/{vm_id}/stop
curl -X POST http://localhost:8200/api/v1/portal/pt_PORTAL_TOKEN/vms/{vm_id}/restart
```

Get console URL (requires `console` level):

```bash
curl http://localhost:8200/api/v1/portal/pt_PORTAL_TOKEN/vms/{vm_id}/console
```

Response:

```json
{
  "url": "wss://abc123.console.example.com/ws/JWT_TOKEN",
  "token": "JWT_TOKEN"
}
```

---

## Part 2 — Full Endpoint Reference

Complete reference for all 170+ Troshka REST API endpoints. Organized by resource.

---

### System

System-level endpoints for health checks, metadata, and debugging.

#### GET /api/v1/health

Health check endpoint.

**Auth:** none  
**Response:** `{ status: "ok" }`

#### GET /api/v1/ocp/versions

List available OpenShift versions for OCP templates.

**Auth:** none  
**Response:** `string[]`

#### GET /api/v1/debug/threads

List all active Python threads (for debugging stuck operations).

**Auth:** admin  
**Response:** `{ threads: [...] }`

---

### Auth

Authentication, user identity, SSH keys, and credentials.

#### GET /api/v1/auth/config

Get authentication config.

**Auth:** none  
**Response:** `{ oauth_enabled: boolean, dev_mode: boolean }`

#### GET /api/v1/auth/dev-token

Get a dev JWT token (admin role). Dev mode only.

**Auth:** none  
**Response:** `{ token: string, user_id: string, email: string, display_name: string, role: string }`

#### GET /api/v1/auth/dev-token/{role}

Get a dev JWT token with specific role (user|operator|admin). Dev mode only.

**Auth:** none  
**Response:** `{ token: string, user_id: string, email: string, display_name: string, role: string }`

#### GET /api/v1/auth/me

Get current user info.

**Auth:** user  
**Response:** `{ id: string, email: string, display_name: string, role: string }`

#### GET /api/v1/auth/ssh-keys

List user's SSH public keys.

**Auth:** user  
**Response:** `[{ id: number, name: string, public_key: string, created_at: string }]`

#### POST /api/v1/auth/ssh-keys

Add a new SSH public key.

**Auth:** user  
**Request:** `{ name: string, public_key: string }`  
**Response:** `{ id: number, name: string }` (201)

#### DELETE /api/v1/auth/ssh-keys/{key_id}

Delete an SSH key.

**Auth:** user  
**Response:** `204 No Content`

#### GET /api/v1/auth/ocp-pull-secret

Get masked OCP pull secret status.

**Auth:** user  
**Response:** `{ has_secret: boolean, masked: string }`

#### PUT /api/v1/auth/ocp-pull-secret

Store OCP pull secret.

**Auth:** user  
**Request:** `{ pull_secret: string }`  
**Response:** `{ status: "saved" }`

#### DELETE /api/v1/auth/ocp-pull-secret

Delete stored OCP pull secret.

**Auth:** user  
**Response:** `204 No Content`

#### GET /api/v1/auth/rh-offline-token

Get masked Red Hat offline token status.

**Auth:** user  
**Response:** `{ has_token: boolean, masked: string }`

#### PUT /api/v1/auth/rh-offline-token

Store Red Hat offline token (for Image Builder).

**Auth:** user  
**Request:** `{ offline_token: string }`  
**Response:** `{ status: "saved" }`

#### PATCH /api/v1/auth/ocp-pull-secret

Toggle pull-through registry mode (enable/disable without changing the secret).

**Auth:** user  
**Request:** `{ pull_through_registry: boolean, pull_through_registry_url?: string, pull_through_registry_user?: string, pull_through_registry_password?: string }`  
**Response:** `{ status: "saved" }`

#### DELETE /api/v1/auth/rh-offline-token

Delete stored Red Hat offline token.

**Auth:** user  
**Response:** `204 No Content`

#### GET /api/v1/auth/registry-credentials

List user's registry credentials (passwords omitted).

**Auth:** user  
**Response:** `[{ id: string, name: string, registry_url: string, username: string, created_at: string }]`

#### POST /api/v1/auth/registry-credentials

Create a registry credential.

**Auth:** user  
**Request:** `{ name: string, registry_url: string, username: string, password: string }`  
**Response:** `RegistryCredentialResponse` (201)

#### PUT /api/v1/auth/registry-credentials/{id}

Update a registry credential.

**Auth:** user  
**Request:** `{ name?: string, registry_url?: string, username?: string, password?: string }`  
**Response:** `RegistryCredentialResponse`

#### DELETE /api/v1/auth/registry-credentials/{id}

Delete a registry credential.

**Auth:** user  
**Response:** `204 No Content`

---

### Providers

Cloud provider accounts (AWS EC2, GCP, Azure, OCP Virt).

#### GET /api/v1/providers/

List all providers.

**Auth:** admin  
**Response:** `ProviderResponse[]`

#### POST /api/v1/providers/

Create a new provider.

**Auth:** admin  
**Request:** `ProviderCreate`  
**Response:** `ProviderResponse` (201)

#### PATCH /api/v1/providers/{provider_id}

Update provider settings.

**Auth:** admin  
**Request:** `ProviderUpdate`  
**Response:** `ProviderResponse`

#### DELETE /api/v1/providers/{provider_id}

Delete a provider.

**Auth:** admin  
**Response:** `204 No Content`

#### POST /api/v1/providers/{provider_id}/test

Test provider credentials.

**Auth:** admin  
**Response:** `{ status: string, ... }`

#### GET /api/v1/providers/{provider_id}/discover-images

List available RHEL 9/10 images (AWS). Returns both Access2 (BYOS) and Hourly (PAYG).

**Auth:** admin  
**Response:** `{ region: string, images: [{ type: string, label: string, image_id: string, name: string, created: string }] }`

#### GET /api/v1/providers/{provider_id}/discover-ami

Deprecated alias for `/discover-images`.

**Auth:** admin

#### GET /api/v1/providers/{provider_id}/discover-vpcs

List troshka-managed VPCs and subnets (AWS).

**Auth:** admin  
**Response:** `{ region: string, vpcs: [{ vpc_id: string, name: string, cidr: string, is_default: boolean, subnets: [...] }] }`

#### POST /api/v1/providers/{provider_id}/create-vpc

Create a new VPC with subnets in all AZs (AWS).

**Auth:** admin  
**Response:** `{ vpc_id: string, subnet_ids: string[], security_group_id: string, internet_gateway_id: string, cidr: string, availability_zones: string[] }`

#### POST /api/v1/providers/{provider_id}/setup-infra

Set VPC/subnet on provider and ensure security group exists (AWS).

**Auth:** admin  
**Request:** `{ vpc_id: string, subnet_id: string }`  
**Response:** `{ vpc_id: string, subnet_id: string, security_group_id: string }`

#### POST /api/v1/providers/{provider_id}/create-network-gcp

Create VPC, subnet, and firewall rules (GCP).

**Auth:** admin  
**Response:** `{ status: "ok", network: string, subnet: string, zone: string }`

#### POST /api/v1/providers/{provider_id}/create-network-azure

Create Resource Group, VNet, subnet, and NSG (Azure).

**Auth:** admin  
**Response:** `{ status: "ok", resource_group: string, vnet: string, subnet: string, nsg: string }`

#### GET /api/v1/providers/{provider_id}/discover-images-gcp

List RHEL BYOS/PAYG images (GCP).

**Auth:** admin  
**Response:** `[{ name: string, self_link: string, family: string, source: string, creation_timestamp: string }]`

#### GET /api/v1/providers/{provider_id}/discover-images-azure

List RHEL BYOS/PAYG images (Azure).

**Auth:** admin  
**Response:** `[{ name: string, urn: string, version: string, source: string, rhel_version: string }]`

#### GET /api/v1/providers/{provider_id}/discover-isos

List ISO PVCs in troshka namespace (OCP Virt).

**Auth:** admin  
**Response:** `{ isos: [{ name: string, size: string }] }`

#### GET /api/v1/providers/{provider_id}/discover-datasources

List available VM base images (DataSources) on OCP Virt cluster.

**Auth:** admin  
**Response:** `{ datasources: [{ name: string, ready: boolean }] }`

#### POST /api/v1/providers/{provider_id}/set-image

Set default host image for provider.

**Auth:** admin  
**Request:** `{ image_id: string }`  
**Response:** `{ image_id: string }`

#### POST /api/v1/providers/{provider_id}/set-ami

Deprecated alias for `/set-image`.

**Auth:** admin

#### POST /api/v1/providers/{provider_id}/set-iso

Set install ISO PVC for OCP Virt provider.

**Auth:** admin  
**Request:** `{ iso_pvc: string }`  
**Response:** `{ iso_pvc: string }`

#### POST /api/v1/providers/{provider_id}/create-bucket

Create S3 bucket (AWS S3 providers).

**Auth:** admin  
**Response:** `{ status: string, bucket: string }`

#### GET /api/v1/providers/{provider_id}/availability-zones

List available AZs in provider's region (AWS).

**Auth:** admin  
**Response:** `string[]`

#### POST /api/v1/providers/{provider_id}/setup-console

Setup console DNS (Route53/Cloud DNS) for VNC proxy.

**Auth:** admin  
**Request:** `{ base_domain: string }`  
**Response:** `{ zone_id: string | null, base_domain: string, nameservers: string[] }`

#### DELETE /api/v1/providers/{provider_id}/console

Remove console DNS configuration.

**Auth:** admin  
**Response:** `{ status: "removed" }`

#### POST /api/v1/providers/{provider_id}/build-image

Build custom RHEL host image via Red Hat Image Builder.

**Auth:** admin  
**Request:** `{ rhel_version: "rhel-9" | "rhel-10" }` (optional)  
**Response:** `{ status: "started", message: string }` (202)

#### GET /api/v1/providers/{provider_id}/build-image/status

Get Image Builder build status.

**Auth:** admin  
**Response:** `{ status: string, progress: number, detail: string, image_id: string | null }`

#### DELETE /api/v1/providers/{provider_id}/build-image/status

Clear Image Builder build status.

**Auth:** admin  
**Response:** `204 No Content`

#### POST /api/v1/providers/{provider_id}/gc

Run EIP garbage collection (release unused EIPs).

**Auth:** admin  
**Response:** `{ released: number }`

---

### Hosts

Physical/virtual hosts running libvirt (EC2 metal, GCE, Azure VMs, OCP Virt VMs).

#### GET /api/v1/hosts/expected-agent-version

Get expected troshkad agent version (hash of troshkad.py).

**Auth:** none  
**Response:** `{ version: string }`

#### GET /api/v1/hosts/overcommit

Get CPU and RAM overcommit ratios.

**Auth:** none  
**Response:** `{ cpu_ratio: number, ram_ratio: number }`

#### GET /api/v1/hosts/

List all hosts (excludes pattern buffers).

**Auth:** operator  
**Response:** `HostResponse[]`

#### GET /api/v1/hosts/storage

Get live disk usage for all active hosts.

**Auth:** operator  
**Response:** `{ [host_id]: { partitions: [...] | used_pct: number, free_gb: number, total_gb: number } }`

#### GET /api/v1/hosts/summary

Host pool summary by region.

**Auth:** operator  
**Response:** `[{ region: string, total_hosts: number, active_hosts: number, total_vcpus: number, alloc_vcpus: number, used_vcpus: number, total_ram_mb: number, alloc_ram_mb: number, used_ram_mb: number }]`

#### POST /api/v1/hosts/

Provision a new host.

**Auth:** admin  
**Request:** `{ provider_id: string, instance_type?: string, region?: string, image_id?: string, storage_pool_id?: string }`  
**Response:** `HostResponse` (201, async)

#### GET /api/v1/hosts/{host_id}

Get host details.

**Auth:** operator  
**Response:** `HostResponse`

#### POST /api/v1/hosts/{host_id}/install-agent

Install/reinstall troshkad agent via SSH.

**Auth:** admin  
**Response:** `{ status: "installing" }` (async)

#### GET /api/v1/hosts/{host_id}/ssh-key

Get SSH private key for host.

**Auth:** admin  
**Response:** `{ key_pair_name: string, private_key: string, public_key?: string, ssh_command?: string, ssh_script_command: string }`

#### GET /api/v1/hosts/{host_id}/ssh-key/download

Download SSH private key as file.

**Auth:** admin  
**Response:** text/plain attachment

#### POST /api/v1/hosts/{host_id}/poweroff

Stop the instance (deallocate).

**Auth:** admin  
**Response:** `{ status: "stopped" }`

#### POST /api/v1/hosts/{host_id}/poweron

Start a stopped instance (optionally resize first).

**Auth:** admin  
**Request:** `{ instance_type?: string }` (optional)  
**Response:** `{ status: "starting" }` (async)

#### POST /api/v1/hosts/{host_id}/resize

Change instance type (requires stopped state).

**Auth:** admin  
**Request:** `{ instance_type: string }`  
**Response:** `{ status: "resized", old_instance_type: string, new_instance_type: string, total_vcpus: number, total_ram_mb: number, max_eips: number }`

#### POST /api/v1/hosts/{host_id}/resize-storage

Grow EBS data volume (online resize).

**Auth:** admin  
**Request:** `{ size_gb: number }`  
**Response:** `{ status: "resized", old_size_gb: number, new_size_gb: number, volume_id: string }`

#### POST /api/v1/hosts/{host_id}/extend-storage

Auto-extend storage by configured increment.

**Auth:** admin  
**Request:** `{ increment_gb?: number }` (optional)  
**Response:** `{ status: "extended", old_size_gb: number, new_size_gb: number }`

#### PATCH /api/v1/hosts/{host_id}

Update host auto-extend config.

**Auth:** admin  
**Request:** `{ auto_extend_enabled?: boolean, auto_extend_threshold_pct?: number, auto_extend_increment_gb?: number, auto_extend_max_gb?: number | null }`  
**Response:** `{ status: "updated" }`

#### POST /api/v1/hosts/{host_id}/gc

Run garbage collector on host.

**Auth:** admin  
**Response:** `{ orphans_found: number, orphans_cleaned: number, ... }`

#### GET /api/v1/hosts/{host_id}/gc/preview

Dry-run GC — show what would be cleaned.

**Auth:** admin  
**Response:** `{ orphans_found: number, ... }`

#### POST /api/v1/hosts/{host_id}/wipe

Destroy all projects on host and clean up resources.

**Auth:** admin  
**Response:** `{ projects_reset: number, projects_destroyed: number, cleanup: { ... } }`

#### POST /api/v1/hosts/{host_id}/update-agent

Push troshkad update to host.

**Auth:** admin  
**Response:** `{ status: "updating", version: string, force: boolean }` (async)

#### POST /api/v1/hosts/{host_id}/evacuate

Move all projects off host to other hosts in same pool.

**Auth:** admin  
**Response:** `{ status: "evacuating", host_id: string, project_count: number }` (async)

#### DELETE /api/v1/hosts/{host_id}

Terminate host instance.

**Auth:** admin  
**Response:** `204 No Content` (async)

---

### Storage Pools

Shared storage pools (FSx, Azure Files, NetApp, BYO NFS) and local pools.

#### GET /api/v1/storage-pools/

List all storage pools.

**Auth:** admin  
**Response:** `StoragePoolResponse[]`

#### GET /api/v1/storage-pools/{pool_id}

Get pool details.

**Auth:** admin  
**Response:** `StoragePoolResponse`

#### POST /api/v1/storage-pools/

Create a storage pool.

**Auth:** admin  
**Request:** `StoragePoolCreate`  
**Response:** `StoragePoolResponse` (201, async for shared modes)

#### PATCH /api/v1/storage-pools/{pool_id}

Update pool config (throughput, NFS endpoint, auto-extend).

**Auth:** admin  
**Request:** `StoragePoolUpdate`  
**Response:** `StoragePoolResponse`

#### POST /api/v1/storage-pools/{pool_id}/extend

Extend FSx filesystem capacity.

**Auth:** admin  
**Request:** `{ increment_gb?: number }` (optional)  
**Response:** `{ status: "extended", old_size_gb: number, new_size_gb: number }` (202 if async)

#### DELETE /api/v1/storage-pools/{pool_id}

Delete pool (must have no hosts).

**Auth:** admin  
**Response:** `204 No Content`

#### GET /api/v1/storage-pools/{pool_id}/cache

List shared cache entries.

**Auth:** admin  
**Response:** `SharedCacheEntryResponse[]`

#### DELETE /api/v1/storage-pools/{pool_id}/cache/{entry_id}

Evict a cache entry.

**Auth:** admin  
**Response:** `204 No Content`

#### POST /api/v1/storage-pools/{pool_id}/probe-azs

Probe AZ capacity for instance types.

**Auth:** admin  
**Request:** `{ instance_types: string[] }`  
**Response:** `{ results: [{ az: string, supported_types: string[], unsupported_types: string[] }], recommended_az: string }`

#### POST /api/v1/storage-pools/{pool_id}/gc

Run GC on pool (shared cache eviction).

**Auth:** admin  
**Request:** query param `dry_run` (optional)  
**Response:** `{ ... }`

#### POST /api/v1/storage-pools/{pool_id}/pattern-buffer

Provision or replace pattern buffer host.

**Auth:** admin  
**Request:** `{ instance_type?: string }` (optional)  
**Response:** `{ status: "provisioning", pool_id: string }` (async)

#### DELETE /api/v1/storage-pools/{pool_id}/pattern-buffer

Terminate pattern buffer.

**Auth:** admin  
**Response:** `{ status: "deleted", pool_id: string }`

#### POST /api/v1/storage-pools/{pool_id}/pattern-buffer/stop

Stop (sleep) pattern buffer.

**Auth:** admin  
**Response:** `{ status: "stopped", pool_id: string }`

#### POST /api/v1/storage-pools/{pool_id}/pattern-buffer/wake

Wake pattern buffer.

**Auth:** admin  
**Response:** `{ status: "connected", pool_id: string }`

---

### Projects

VM topology projects.

#### GET /api/v1/projects/

List user's projects.

**Auth:** user  
**Response:** `ProjectResponse[]`

#### POST /api/v1/projects/

Create a new project.

**Auth:** user  
**Request:** `ProjectCreate`  
**Response:** `ProjectResponse` (201)

#### GET /api/v1/projects/templates

List topology templates.

**Auth:** user  
**Response:** `[{ id: string, display_name: string, description: string, ... }]`

#### POST /api/v1/projects/from-template

Create project from template (e.g., OCP IPI/Agent).

**Auth:** user  
**Request:** `{ template_id: string, name: string, cluster_name: string, base_domain: string, ocp_version: string, bastion_password: string, bastion_image_id?: string, bastion_iso_id?: string, bastion_ssh_key_id?: number, auto_install_ocp?: boolean, external_access?: boolean, block_outbound?: boolean, ssh_pub_key?: string, ... }`  
**Response:** `{ id: string, name: string }` (201)

`ssh_pub_key` injects an SSH public key directly (for agnosticd key injection without requiring a stored key ID).

#### GET /api/v1/projects/{project_id}

Get project details.

**Auth:** user  
**Response:** `{ id, name, description, owner_id, provider_id, host_type, host_id, guid, state, topology, deployed_topology, vni_map, deploy_error, ocp_status, ocp_install_elapsed, tags, run_timer_hours, lifetime_expires_at, poweroff_mode, created_at, updated_at, bmc?: { ... } }`

#### PATCH /api/v1/projects/{project_id}

Update project (name, description, topology, etc.).

**Auth:** user  
**Request:** `ProjectUpdate`  
**Response:** `ProjectResponse`

#### POST /api/v1/projects/{project_id}/deploy

Deploy project to a host.

**Auth:** user  
**Request:** query params `storage_pool_id` and `host_id` (optional, admin only)  
**Response:** `{ status: "deploying", host_id: string, host_ip: string, requirements: { ... } }` (202, async)

#### GET /api/v1/projects/{project_id}/deploy-progress

Get deploy progress.

**Auth:** user  
**Response:** `{ state: string, progress: { total: number, completed: number, status: string, detail: string, active_operations: [...] } }`

#### POST /api/v1/projects/{project_id}/stop

Stop all VMs (graceful shutdown).

**Auth:** user  
**Response:** `{ status: "stopping" }` (async)

#### POST /api/v1/projects/{project_id}/force-stop

Force-stop all VMs (destroy).

**Auth:** user  
**Response:** `{ status: "stopped" }`

#### POST /api/v1/projects/{project_id}/start

Start a stopped project.

**Auth:** user  
**Response:** `{ status: "starting" }` (async)

#### POST /api/v1/projects/{project_id}/reconfigure

Apply topology changes (boot order, CPU, RAM, add/remove VMs) without destroying disks.

**Auth:** user  
**Request:** `{ restart_vm_ids?: string[] }` (optional)  
**Response:** `{ status: "reconfiguring" }` (async)

#### POST /api/v1/projects/{project_id}/redeploy

Destroy existing deployment and redeploy with current topology.

**Auth:** user  
**Response:** `{ status: "deploying", host_id: string, host_ip: string, requirements: { ... } }` (async)

#### POST /api/v1/projects/{project_id}/undeploy

Destroy all infrastructure and reset to draft.

**Auth:** user  
**Response:** `{ status: "draft" }`

#### DELETE /api/v1/projects/{project_id}

Delete project (destroys infrastructure if deployed).

**Auth:** user  
**Response:** `204 No Content`

#### GET /api/v1/projects/{project_id}/export-template

Export project topology as YAML template.

**Auth:** user  
**Response:** `text/yaml` — template YAML including VMs, networks, storage, OCP metadata, BMC config

#### POST /api/v1/projects/{project_id}/import-vm

Import a VM from a snapshot into project topology.

**Auth:** user  
**Request:** `{ snapshot_id: string, position_x: number, position_y: number }`  
**Response:** `ProjectResponse`

#### POST /api/v1/projects/{project_id}/migrate

Live-migrate project to another host in same pool.

**Auth:** admin  
**Request:** `{ target_host_id: string }`  
**Response:** `{ status: "migrating", project_id: string, target_host_id: string }` (async)

---

### VMs

VM-level operations within a project.

#### GET /api/v1/projects/{project_id}/vm-states

Get actual libvirt state of all VMs.

**Auth:** user  
**Response:** `{ states: { [vm_id]: "running" | "stopped" | "not_found" | ... }, progress: { [vm_id]: { step, detail } } }`

#### POST /api/v1/projects/{project_id}/vms/{vm_id}/start

Start a VM.

**Auth:** user  
**Response:** `{ action: "start", success: boolean, starting_project?: boolean }` (async if project stopped)

#### POST /api/v1/projects/{project_id}/vms/{vm_id}/stop

Stop a VM (graceful shutdown).

**Auth:** user  
**Response:** `{ action: "stop", success: boolean }`

#### GET /api/v1/projects/{project_id}/vms/{vm_id}/status

Get VM status and boot devices.

**Auth:** user  
**Response:** `{ state: string, boot_devs: string[] }`

#### POST /api/v1/projects/{project_id}/vms/{vm_id}/forcestop

Force-stop a VM (virsh destroy).

**Auth:** user  
**Response:** `{ action: "forcestop", success: boolean }`

#### POST /api/v1/projects/{project_id}/vms/{vm_id}/restart

Restart a VM (reboot).

**Auth:** user  
**Response:** `{ action: "restart", success: boolean }`

#### GET /api/v1/projects/{project_id}/vms/{vm_id}/console

Get VNC console WebSocket URL.

**Auth:** user  
**Response:** `{ ws_url: string }` or `{ error: string }`

#### POST /api/v1/projects/{project_id}/vms/{vm_id}/exec

Execute a command on VM via SSH or serial console.

**Auth:** user  
**Request:** `{ command: string, method?: "serial"|"ssh"|"auto", username?: string, password?: string, ssh_key_id?: number, timeout?: number }`  
**Response:** `{ output: string, error?: string, exit_code?: number }`

`method`: `serial` (always works, no network), `ssh` (requires network + credentials), `auto` (tries SSH first, falls back to serial). SSH key auth is preferred over password when `ssh_key_id` is provided.

#### PUT /api/v1/projects/{project_id}/vms/{vm_id}/files

Upload a file to VM via SCP.

**Auth:** user  
**Request:** multipart/form-data with file upload, query params `remote_path`, `mode`, `username`, `password`  
**Response:** `{ status: "success" }`

#### GET /api/v1/projects/{project_id}/vms/{vm_id}/files

Download a file from VM via SCP.

**Auth:** user  
**Request:** query params `remote_path`, `username`, `password`  
**Response:** application/octet-stream attachment

#### POST /api/v1/projects/{project_id}/vms/{vm_id}/redeploy

Destroy and recreate a single VM.

**Auth:** user  
**Response:** `{ status: "redeploying" }` (async)

#### POST /api/v1/projects/{project_id}/vms/{vm_id}/cancel-redeploy

Cancel a stuck VM redeploy.

**Auth:** user  
**Response:** `{ status: "cancelled" }`

#### POST /api/v1/projects/{project_id}/vms/{vm_id}/snapshot

Create a snapshot of a VM (saves to library).

**Auth:** user  
**Request:** `{ name: string }`  
**Response:** `LibraryItem` (202, async)

---

### Disks

Disk-level operations (deprecated — disks are managed via topology in canvas).

#### GET /api/v1/projects/{project_id}/disks/

List disks in a project.

**Auth:** user  
**Response:** `Disk[]`

#### POST /api/v1/projects/{project_id}/disks/

Create a disk.

**Auth:** user  
**Request:** `DiskCreate`  
**Response:** `Disk` (201)

#### GET /api/v1/projects/{project_id}/disks/{disk_id}

Get disk details.

**Auth:** user  
**Response:** `Disk`

#### PATCH /api/v1/projects/{project_id}/disks/{disk_id}

Update disk.

**Auth:** user  
**Request:** `DiskUpdate`  
**Response:** `Disk`

#### DELETE /api/v1/projects/{project_id}/disks/{disk_id}

Delete a disk.

**Auth:** user  
**Response:** `204 No Content`

#### POST /api/v1/projects/{project_id}/disks/{disk_id}/attach/{vm_id}

Attach disk to VM.

**Auth:** user  
**Response:** `{ status: "attached" }`

#### POST /api/v1/projects/{project_id}/disks/{disk_id}/detach

Detach disk from VM.

**Auth:** user  
**Response:** `{ status: "detached" }`

---

### Networks

Network-level operations (deprecated — networks are managed via topology in canvas).

#### GET /api/v1/projects/{project_id}/networks/

List networks in a project.

**Auth:** user  
**Response:** `Network[]`

#### POST /api/v1/projects/{project_id}/networks/

Create a network.

**Auth:** user  
**Request:** `NetworkCreate`  
**Response:** `Network` (201)

#### GET /api/v1/projects/{project_id}/networks/{network_id}

Get network details.

**Auth:** user  
**Response:** `Network`

#### PATCH /api/v1/projects/{project_id}/networks/{network_id}

Update network.

**Auth:** user  
**Request:** `NetworkUpdate`  
**Response:** `Network`

#### DELETE /api/v1/projects/{project_id}/networks/{network_id}

Delete a network.

**Auth:** user  
**Response:** `204 No Content`

---

### EIPs

Elastic/static IP addresses and port forwarding.

#### GET /api/v1/projects/{project_id}/eips

List EIPs allocated to project.

**Auth:** user  
**Response:** `[{ id: string, name: string, ip: string, vm_id: string, port_forwards: [...] }]`

#### DELETE /api/v1/projects/{project_id}/eips/{canvas_eip_id}

Release an EIP.

**Auth:** user  
**Response:** `{ status: "released" }`

#### POST /api/v1/projects/{project_id}/eips/sync

Sync security group rules for project EIPs.

**Auth:** user  
**Response:** `{ status: "synced" }`

---

### Library

Personal and shared ISOs/images (S3-backed).

#### GET /api/v1/library/

List library items.

**Auth:** user  
**Request:** query params `type`, `q`  
**Response:** `LibraryItem[]`

#### GET /api/v1/library/{item_id}

Get library item details.

**Auth:** user  
**Response:** `LibraryItem`

#### POST /api/v1/library/

Create a new library item entry.

**Auth:** user  
**Request:** `LibraryItemCreate`  
**Response:** `LibraryItem` (201)

#### PATCH /api/v1/library/{item_id}

Update library item metadata.

**Auth:** user  
**Request:** `LibraryItemUpdate`  
**Response:** `LibraryItem`

#### DELETE /api/v1/library/{item_id}

Delete a library item (removes from S3).

**Auth:** user  
**Response:** `204 No Content`

#### POST /api/v1/library/{item_id}/upload-start

Start multipart upload.

**Auth:** user  
**Response:** `{ upload_id: string }`

#### POST /api/v1/library/{item_id}/upload-part-url

Get presigned URL for uploading a part.

**Auth:** user  
**Request:** `{ upload_id: string, part_number: number }`  
**Response:** `{ url: string, part_number: number }`

#### POST /api/v1/library/{item_id}/upload-complete

Complete multipart upload.

**Auth:** user  
**Request:** `{ upload_id: string, parts: [{ part_number: number, etag: string }] }`  
**Response:** `{ status: "complete" }`

#### POST /api/v1/library/{item_id}/import-url

Import from public URL.

**Auth:** user  
**Request:** `{ url: string }`  
**Response:** `{ status: "importing" }` (202, async)

#### POST /api/v1/library/{item_id}/cancel

Cancel an in-progress upload or import.

**Auth:** user  
**Response:** `{ status: "cancelled" }`

#### POST /api/v1/library/{item_id}/share

Share library item with another user.

**Auth:** user  
**Request:** `{ email: string }`  
**Response:** `{ status: "shared" }`

#### DELETE /api/v1/library/{item_id}/share/{user_email}

Unshare library item.

**Auth:** user  
**Response:** `{ status: "unshared" }`

#### POST /api/v1/library/scan-s3

Scan S3 bucket for orphaned objects and update DB (admin operation).

**Auth:** admin  
**Response:** `{ found: number, imported: number }`

---

### Patterns

Reusable VM topologies with disk images.

#### GET
 /api/v1/patterns/

List patterns owned by or shared with user.

**Auth:** user  
**Request:** query params `q`, `shared`  
**Response:** `PatternResponse[]`

#### POST /api/v1/patterns/

Create a pattern (from project or blank).

**Auth:** user  
**Request:** `PatternCreate`  
**Response:** `PatternResponse` (201)

#### GET /api/v1/patterns/{pattern_id}

Get pattern details.

**Auth:** user  
**Response:** `PatternResponse`

#### PATCH /api/v1/patterns/{pattern_id}

Update pattern metadata.

**Auth:** user  
**Request:** `PatternUpdate`  
**Response:** `PatternResponse`

#### DELETE /api/v1/patterns/{pattern_id}

Delete a pattern (removes from S3).

**Auth:** user  
**Response:** `204 No Content`

#### POST /api/v1/patterns/{pattern_id}/share

Share pattern with another user.

**Auth:** user  
**Request:** `{ email: string }`  
**Response:** `{ status: "shared" }`

#### DELETE /api/v1/patterns/{pattern_id}/share/{user_email}

Unshare pattern.

**Auth:** user  
**Response:** `{ status: "unshared" }`

#### GET /api/v1/patterns/{pattern_id}/progress

Get pattern capture progress.

**Auth:** user  
**Response:** `{ state: string, progress: number, detail: string }`

#### POST /api/v1/patterns/{pattern_id}/deploy

Deploy pattern to new project.

**Auth:** user  
**Request:** `{ name: string, description?: string, storage_pool_id?: string, host_id?: string, common_password?: string }`  
**Response:** `ProjectResponse` (201, async)

`common_password` overrides BMC and cloud-init credentials baked in the pattern's topology.

#### POST /api/v1/patterns/bulk-deploy

Deploy multiple copies of a pattern.

**Auth:** user  
**Request:** `{ pattern_id: string, count: number, name_prefix: string, storage_pool_id?: string }`  
**Response:** `{ projects: ProjectResponse[] }` (async)

---

### DNS Providers

External DNS providers (Route53, etc.) for DNS record management.

#### GET /api/v1/dns-providers/

List DNS providers.

**Auth:** user  
**Response:** `DnsProviderResponse[]`

#### POST /api/v1/dns-providers/

Create a DNS provider.

**Auth:** user  
**Request:** `DnsProviderCreate`  
**Response:** `DnsProviderResponse` (201)

#### GET /api/v1/dns-providers/{provider_id}

Get DNS provider details.

**Auth:** user  
**Response:** `DnsProviderResponse`

#### PATCH /api/v1/dns-providers/{provider_id}

Update DNS provider.

**Auth:** user  
**Request:** `DnsProviderUpdate`  
**Response:** `DnsProviderResponse`

#### DELETE /api/v1/dns-providers/{provider_id}

Delete a DNS provider.

**Auth:** user  
**Response:** `204 No Content`

---

### API Keys

API key management for programmatic access.

#### GET /api/v1/api-keys/

List user's API keys.

**Auth:** user  
**Response:** `[{ id: string, name: string, prefix: string, created_at: string, last_used: string | null }]`

#### POST /api/v1/api-keys/

Create a new API key.

**Auth:** user  
**Request:** `{ name: string }`  
**Response:** `{ id: string, name: string, key: string }` (201)

#### DELETE /api/v1/api-keys/{key_id}

Delete an API key.

**Auth:** user  
**Response:** `204 No Content`

---

### Portal

Portal access (share projects via token URLs).

#### POST /api/v1/portal/access

Grant portal access to a project.

**Auth:** user  
**Request:** `{ project_id: string }`  
**Response:** `{ token: string, url: string }`

#### POST /api/v1/portal/revoke

Revoke portal access token.

**Auth:** user  
**Request:** `{ token: string }`  
**Response:** `{ status: "revoked" }`

#### GET /api/v1/portal/project/{token}

Get project details via portal token.

**Auth:** none (token in URL)  
**Response:** `{ id, name, topology, vm_states: { ... }, ... }`

#### POST /api/v1/portal/vm/{token}/{vm_id}/exec

Execute command on VM via portal token.

**Auth:** none (token in URL)  
**Request:** `{ command: string, username?: string, password?: string, timeout?: number }`  
**Response:** `{ output: string, error?: string, exit_code?: number }`

---

### Templates

Deploy pre-built topology templates (OCP, etc.).

#### POST /api/v1/templates/deploy-template

Deploy a topology template as a new project.

**Auth:** user  
**Request:** `{ template_id: string, ...template-specific fields }`  
**Response:** `{ id: string, name: string }` (201, async)

---

### WebSocket

Real-time subscriptions for projects and patterns.

#### WS /api/v1/ws/projects

Subscribe to project updates (VM states, deploy progress).

**Auth:** user (token query param)  
**Messages:** `{ type: "project_update", project_id: string, data: { ... } }`

#### WS /api/v1/ws/patterns

Subscribe to pattern updates (capture progress, state changes).

**Auth:** user (token query param)  
**Messages:** `{ type: "pattern_update", pattern_id: string, data: { ... } }`

---

## Response Codes

Troshka uses standard HTTP status codes:

- **200 OK**: Successful GET/PATCH/PUT/POST (sync operations)
- **201 Created**: Resource created successfully
- **202 Accepted**: Async operation started (check progress endpoint)
- **204 No Content**: Successful DELETE or void operation
- **400 Bad Request**: Invalid request body, missing required fields, validation failed
- **401 Unauthorized**: Missing or invalid auth token
- **403 Forbidden**: Insufficient permissions (role-based access control)
- **404 Not Found**: Resource does not exist or user lacks access
- **409 Conflict**: Resource already exists (duplicate name, concurrent modification)
- **500 Internal Server Error**: Unexpected server error (check backend logs)
- **503 Service Unavailable**: Host agent disconnected, external service unavailable

---

## Error Response Format

All error responses (4xx, 5xx) return JSON:

```json
{
  "detail": "Human-readable error message"
}
```

For validation errors (422 Unprocessable Entity):

```json
{
  "detail": [
    {
      "loc": ["body", "field_name"],
      "msg": "Field required",
      "type": "value_error.missing"
    }
  ]
}
```

---

## Rate Limiting

No rate limiting is currently enforced. Heavy operations (deploy, pattern capture) use server-side job queuing to prevent resource exhaustion.

---

## Pagination

Endpoints that return lists use cursor-based pagination where applicable (S3 multipart uploads, large library listings). Most endpoints return all results (filtered by user ownership).

---

## Async Operations

Long-running operations return HTTP 202 Accepted with a status check endpoint:

- **Deploy**: `GET /api/v1/projects/{id}/deploy-progress`
- **Pattern capture**: `GET /api/v1/patterns/{id}/progress`
- **Library import**: `GET /api/v1/library/{id}` (check `status` field)
- **Host provision**: `GET /api/v1/hosts/{id}` (check `state` field)

Clients should poll these endpoints every 2-5 seconds until complete.

---

## WebSocket Subscriptions

For real-time updates, use WebSocket endpoints:

- **Projects**: `/api/v1/ws/projects?token={jwt}`
- **Patterns**: `/api/v1/ws/patterns?token={jwt}`

Messages are JSON with `type` and `data` fields. Reconnect on disconnect (exponential backoff recommended).

---

## Authentication

All endpoints except `/auth/config`, `/auth/dev-token`, `/health`, and `/portal/*` require authentication.

### JWT Tokens

Include in request headers:

```
Authorization: Bearer <jwt_token>
```

Tokens expire after 7 days (configurable). Obtain via `/auth/dev-token` (dev mode) or OAuth proxy (production).

### API Keys

Alternatively, use API keys for programmatic access:

```
X-API-Key: <api_key>
```

Create API keys via `/api-keys/` endpoint.

---

## CORS

CORS is enabled for all origins in dev mode. In production, configure `TROSHKA_ALLOWED_ORIGINS` to restrict cross-origin access.
