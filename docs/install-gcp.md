# Troshka Installation — GCP

This guide covers the GCP-specific setup for Troshka. Complete the [Common Setup](install-common.md) guide first before proceeding.

## Prerequisites

Before setting up Troshka on GCP, ensure you have:

- **GCP Project** — ideally created under an organization folder for policy management
- **GCP APIs Enabled**:
  - Compute Engine API
  - Cloud DNS API
- **gcloud CLI** — installed and authenticated
- **Troshka Backend Running** — see [Common Setup](install-common.md)

Verify your gcloud authentication and active project:

```bash
gcloud auth list
gcloud config get-value project
```

## Service Account Setup

Create a dedicated service account for Troshka with Compute Admin and DNS Admin roles:

```bash
# Set your project ID
export PROJECT_ID=your-project-id

# Create the service account
gcloud iam service-accounts create troshka \
  --project=$PROJECT_ID \
  --display-name="Troshka"

# Grant Compute Admin role
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:troshka@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/compute.admin"

# Grant DNS Admin role (for console setup)
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:troshka@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/dns.admin"

# Create and download the service account key
gcloud iam service-accounts keys create troshka-sa.json \
  --iam-account=troshka@${PROJECT_ID}.iam.gserviceaccount.com
```

Keep the `troshka-sa.json` file secure — you will need its contents for the next step.

## Organization Policy Constraints

If your GCP project is under an organization, verify that you can provision N2-highmem instance types (required for nested virtualization):

```bash
# Check available machine types in your preferred zone
gcloud compute machine-types list \
  --zones=us-central1-a \
  --filter="name:n2-highmem-*"
```

If N2-highmem types are blocked by an org policy constraint (e.g., `custom.denyCostlyMachineTypes`), you may need to:

1. Request an exception for your project
2. Use E2-standard or N2-standard types (works for pattern buffer hosts, but not for nested virt hosts)
3. Move your project to a folder without the constraint

For development and testing, E2-standard types work fine. For production host provisioning with nested virtualization, N2-highmem types are strongly recommended.

## Create Provider

Use the Troshka API to create a GCP provider. First, get the full contents of `troshka-sa.json`:

```bash
cat troshka-sa.json | jq -c .
```

Then create the provider via curl:

```bash
export TOKEN="your-jwt-token"  # Get from Troshka login

curl -X POST http://localhost:8200/api/v1/providers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "gcp-prod",
    "type": "gcp",
    "credentials": {
      "service_account_json": <paste JSON from troshka-sa.json here>
    },
    "default_region": "us-central1"
  }'
```

Alternatively, use the Troshka admin UI:

1. Navigate to **Admin → Providers**
2. Click **Add Provider**
3. Select **Type: GCP**
4. Paste the contents of `troshka-sa.json` into the **Service Account JSON** field
5. Set **Default Region** (e.g., `us-central1`)
6. Click **Create**

### Test Provider

Verify the provider credentials are working:

```bash
curl -X POST http://localhost:8200/api/v1/providers/{provider_id}/test \
  -H "Authorization: Bearer $TOKEN"
```

A successful response indicates that Troshka can authenticate with GCP using the service account credentials.

## Network Setup

Create a custom VPC network, subnet, and firewall rules for Troshka hosts. This is a one-time setup per provider.

```bash
curl -X POST http://localhost:8200/api/v1/providers/{provider_id}/create-network-gcp \
  -H "Authorization: Bearer $TOKEN"
```

This creates:

- **VPC Network**: `troshka-vpc` (custom mode, no auto-created subnets)
- **Subnet**: `troshka-subnet` with CIDR `10.100.1.0/24`
- **Firewall Rules**:
  - `troshka-allow-ssh`: TCP port 22 (SSH access)
  - `troshka-allow-console`: TCP port 443 (VNC console via HTTPS)
  - `troshka-allow-agent`: TCP port 31337 (troshkad agent)
  - `troshka-allow-vxlan`: UDP port 4789 (VXLAN tunneling for multi-host networking)

All firewall rules target instances with the `troshka-host` network tag. The Troshka provisioner automatically tags all instances with `troshka-host` during creation.

Verify the network was created:

```bash
gcloud compute networks describe troshka-vpc --project=$PROJECT_ID
gcloud compute firewall-rules list --project=$PROJECT_ID --filter="name~troshka"
```

## Host Image Setup

Troshka supports two image modes for GCP:

1. **PAYG (Recommended for dev)** — RHEL images from `rhel-cloud` project, repos work out of the box, no RHSM registration required, slightly higher hourly cost
2. **Image Builder BYOS** — Custom RHEL images built via Red Hat Image Builder with packages pre-installed, lower cost, requires offline token setup

### Option 1: PAYG Images (Default)

Discover available RHEL PAYG images:

```bash
curl -X GET http://localhost:8200/api/v1/providers/{provider_id}/discover-images-gcp \
  -H "Authorization: Bearer $TOKEN"
```

This returns a list of RHEL 9 and RHEL 10 images from `rhel-cloud`. Select the latest RHEL 9 LVM image (e.g., `rhel-9-v20260615`).

Set the default image:

```bash
curl -X POST http://localhost:8200/api/v1/providers/{provider_id}/set-image \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "image_id": "projects/rhel-cloud/global/images/rhel-9-v20260615"
  }'
```

PAYG images include Red Hat subscriptions in the hourly instance cost. They work immediately without any RHSM registration or activation.

### Option 2: Image Builder BYOS (Production)

For production deployments, use Red Hat Image Builder to create custom RHEL BYOS images with all packages pre-installed. This eliminates:

- RHSM registration at boot time
- Package download time during host provisioning
- PAYG image premium

To use Image Builder:

1. Navigate to **Settings** in the Troshka UI
2. Save your **Red Hat Offline Token** (get from https://access.redhat.com/management/api)
3. Go to **Admin → Providers → [Your GCP Provider]**
4. Click **Build Host Image**
5. Wait 10-15 minutes for the image build to complete
6. The image will be automatically set as the `default_image` once ready

Image Builder creates the image in Red Hat's GCP project and shares it with your service account. The build includes all required packages: qemu-kvm, libvirt, dnsmasq, nftables, xorriso, etc.

## Console Setup

Set up Cloud DNS for VNC console access via HTTPS. This creates a DNS zone and configures Let's Encrypt TLS certificates.

```bash
curl -X POST http://localhost:8200/api/v1/providers/{provider_id}/setup-console \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "base_domain": "console.example.com"
  }'
```

The response includes nameservers for the newly created Cloud DNS zone:

```json
{
  "console_base_domain": "console.example.com",
  "console_zone_id": "console-example-com",
  "console_nameservers": [
    "ns-cloud-a1.googledomains.com.",
    "ns-cloud-a2.googledomains.com.",
    "ns-cloud-a3.googledomains.com.",
    "ns-cloud-a4.googledomains.com."
  ]
}
```

Add NS records for the subdomain in your parent DNS zone (wherever `example.com` is hosted):

```
console.example.com.  IN  NS  ns-cloud-a1.googledomains.com.
console.example.com.  IN  NS  ns-cloud-a2.googledomains.com.
console.example.com.  IN  NS  ns-cloud-a3.googledomains.com.
console.example.com.  IN  NS  ns-cloud-a4.googledomains.com.
```

Each host will get an A record in this zone: `{instance_id}.console.example.com` pointing to the host's public IP.

Let's Encrypt TLS certificates are provisioned automatically on each host using the `certbot-dns-google` plugin and the service account credentials for DNS-01 challenge validation.

## Provision First Host

Create your first Troshka host instance:

```bash
curl -X POST http://localhost:8200/api/v1/hosts \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "provider_id": "your-provider-id",
    "instance_type": "n2-highmem-16",
    "storage_size_gb": 500
  }'
```

Recommended instance types:

| Type | vCPUs | RAM (GiB) | Use Case |
|------|-------|-----------|----------|
| n2-highmem-4 | 4 | 32 | Light testing, 1-2 small labs |
| n2-highmem-8 | 8 | 64 | Small production, 2-3 concurrent labs |
| n2-highmem-16 | 16 | 128 | Medium production, 3-5 concurrent labs |
| n2-highmem-32 | 32 | 256 | Large production, 10+ concurrent labs |
| e2-standard-2 | 2 | 8 | Pattern buffer only (no nested virt) |

Host provisioning typically takes 3-5 minutes. The Troshka agent (troshkad) will auto-install and connect to the backend once the instance boots.

### Host Details

Each provisioned host includes:

- **OS**: RHEL 9 (PAYG from `rhel-cloud` or custom Image Builder image)
- **SSH User**: `troshka` (key generated during provisioning, stored in database)
- **Data Disk**: `/dev/sdb` (second attached persistent SSD, formatted as ext4, mounted at `/var/lib/troshka/`)
- **Network Tag**: `troshka-host` (required for firewall rules)
- **Nested Virtualization**: Enabled via `advancedMachineFeatures.enableNestedVirtualization` (except on pattern buffer hosts)
- **Host Maintenance**: Set to `TERMINATE` for nested virt hosts (GCP doesn't support live migration with nested virt)

### Resizing Hosts

To resize a host to a different instance type, use the resize API:

```bash
curl -X POST http://localhost:8200/api/v1/hosts/{host_id}/resize \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "new_instance_type": "n2-highmem-32"
  }'
```

GCP requires the instance to be stopped before resizing, then restarted. The API handles this automatically.

### Extending Storage

To extend the data disk size on a running host:

```bash
curl -X POST http://localhost:8200/api/v1/hosts/{host_id}/extend-storage \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "increment_gb": 100
  }'
```

GCP supports online disk resizes. The backend will:

1. Resize the GCP persistent disk
2. Trigger a filesystem resize on the host via troshkad

Auto-extend can be enabled on the host to automatically grow the disk when usage exceeds a threshold.

## Shared Storage

Troshka supports shared storage pools for live migration between hosts. GCP shared storage options include:

- **Google Filestore** (managed NFS)
- **NetApp Cloud Volumes ONTAP**
- **Self-managed NFS server**

**Note**: Filestore and NetApp are currently blocked by organization policy constraints in some deployments. For now, use **local storage mode** (default) with pattern buffer for pattern save operations.

To provision a pattern buffer host (dedicated storage worker for capturing patterns):

```bash
curl -X POST http://localhost:8200/api/v1/hosts \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "provider_id": "your-provider-id",
    "instance_type": "e2-standard-2",
    "storage_size_gb": 200,
    "host_type": "pattern_buffer"
  }'
```

Pattern buffer hosts:

- Use E2-standard instance types (no nested virt needed)
- Cannot host running VMs (pattern save only)
- Provide fast local NVMe-backed captures before uploading to S3
- Support migration of running VMs between primary hosts during pattern save

## Verification

After completing setup, verify your Troshka deployment:

1. **Provider Test**:
   ```bash
   curl -X POST http://localhost:8200/api/v1/providers/{provider_id}/test \
     -H "Authorization: Bearer $TOKEN"
   ```
   Expected: `{"status": "ok"}`

2. **Network Created**:
   ```bash
   gcloud compute networks describe troshka-vpc --project=$PROJECT_ID
   gcloud compute firewall-rules list --filter="name~troshka" --project=$PROJECT_ID
   ```
   Expected: VPC network with subnet `10.100.1.0/24` and 4 firewall rules

3. **Host Connected**:
   ```bash
   curl -X GET http://localhost:8200/api/v1/hosts \
     -H "Authorization: Bearer $TOKEN"
   ```
   Expected: Host with `status: "connected"` and `agent_connected: true`

4. **Console Working**:
   Open a browser to `https://{instance_id}.{console_base_domain}` (replace with your actual console hostname from the hosts list). You should see a "No active console sessions" page or a live VNC console if VMs are running.

5. **Deploy Test**:
   - Create a new project in the Troshka UI
   - Add a VM to the canvas
   - Click **Deploy**
   - Verify the VM deploys successfully and appears in the VMs list
   - Test console access by clicking the VM's console icon

## Troubleshooting

### Service Account Permission Errors

If you see permission errors during provider test or host provisioning:

```bash
# Verify the service account has the correct roles
gcloud projects get-iam-policy $PROJECT_ID \
  --flatten="bindings[].members" \
  --filter="bindings.members:serviceAccount:troshka@${PROJECT_ID}.iam.gserviceaccount.com"
```

Expected roles: `roles/compute.admin` and `roles/dns.admin`

### Org Policy Blocks Instance Type

If host provisioning fails with "instance type not allowed" or similar:

```bash
# Check org policy constraints
gcloud resource-manager org-policies list --project=$PROJECT_ID

# Describe a specific constraint
gcloud resource-manager org-policies describe \
  compute.vmExternalIpAccess --project=$PROJECT_ID
```

Contact your GCP org admin to request an exception or use allowed instance types.

### Firewall Rules Not Applied

If you can't SSH to hosts or access the console:

```bash
# Verify the instance has the correct network tag
gcloud compute instances describe {instance-id} \
  --zone=us-central1-a \
  --format="value(tags.items)"
```

Expected: `troshka-host` in the tags list

### Console Certificate Issues

If the console shows certificate errors:

1. Verify DNS propagation:
   ```bash
   dig +short {instance_id}.{console_base_domain}
   ```
   Should return the host's public IP

2. Check certbot status on the host (via SSH):
   ```bash
   sudo certbot certificates
   ```

3. Force renewal if needed:
   ```bash
   sudo certbot renew --force-renewal
   ```

### Agent Won't Connect

If the host status shows "disconnected" after 5+ minutes:

1. Verify the instance is running:
   ```bash
   gcloud compute instances describe {instance-id} --zone=us-central1-a
   ```

2. SSH to the host and check agent logs:
   ```bash
   ssh -i /path/to/private-key troshka@{public-ip}
   sudo journalctl -u troshka-agent -f
   ```

3. Restart the agent:
   ```bash
   sudo systemctl restart troshka-agent
   ```

## Next Steps

Your Troshka installation on GCP is now ready. You can:

- **Create Projects** — Build nested VM environments in the Troshka UI
- **Add Library Items** — Upload ISOs and disk images to the library
- **Save Patterns** — Capture lab environments as reusable patterns
- **Configure Quotas** — Set resource limits for users
- **Enable OIDC** — Set up SSO authentication (see [Common Setup](install-common.md))
- **Set Up Monitoring** — Configure health checks and alerts

For production deployments, see the [Common Setup](install-common.md) guide for OIDC configuration, reverse proxy setup, and systemd service management.
