# EIP Allocation & Inbound Connections Design

## Overview

Add Elastic IP (EIP) lifecycle management to Troshka so users can allocate public IPs for inbound connections to their project VMs via gateway port-forwarding. Includes a new provider-level garbage collector for AWS resource cleanup.

## Context

- All AWS public IPv4 addresses cost $0.005/hr (~$3.60/mo) whether EIP or auto-assigned — no cheaper alternative exists
- EIPs are the only way to get a static/reserved public IP in AWS
- The existing `ExternalIpsPanel` and `extIpId` in port-forward rules are placeholders — backend doesn't allocate real AWS resources yet
- Host public IPs remain auto-assigned (free EC2 public IP); EIPs are exclusively for project gateway traffic

## Three Features

1. **EIP Lifecycle** — allocate, tag, associate, disassociate, release via boto3
2. **Gateway Inbound Wiring** — secondary private IPs on host ENI, nftables DNAT, dynamic SG rules
3. **Provider-Level GC** — scan Troshka-tagged AWS resources, reconcile against DB, clean orphans

---

## 1. Data Model

### New table: `elastic_ips`

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `provider_id` | FK → providers | Which AWS account |
| `project_id` | FK → projects, nullable | Which project owns this EIP |
| `canvas_eip_id` | String | Maps to `ExternalIp.id` in topology JSONB |
| `allocation_id` | String | AWS allocation ID |
| `public_ip` | String | The actual EIP address |
| `private_ip` | String, nullable | Secondary private IP on host ENI |
| `host_id` | FK → hosts, nullable | Which host it's currently associated with |
| `association_id` | String, nullable | AWS association ID (needed for disassociate) |
| `state` | String | `allocated` / `associated` / `releasing` |
| `tags` | JSONB | Copy of AWS tags for quick reference |
| `created_at` | DateTime | |

### Changes to existing models

- **Host**: add `max_eips` (Integer) — computed from instance type on provision via `ec2.describe_instance_types` → `MaxIpAddressesPerInterface - 1` (minus 1 for primary private IP). For m8i.xlarge = 14.
- **Project**: no changes — EIP data lives in `elastic_ips` table

### AWS Tags on every EIP

```
ManagedBy: troshka
troshka-provider-id: <provider_id>
troshka-project-id: <project_id>
troshka-canvas-eip-id: <canvas_eip_id>
```

---

## 2. EIP Service (`services/eip_service.py`)

Dedicated service module — separate from provisioner because EIP lifecycle is tied to projects, not hosts.

### Core operations

**`allocate_eip(provider, project_id, canvas_eip_id) → ElasticIp`**
- `ec2.allocate_address(Domain='vpc')`
- Tag immediately (before any other operation — crash safety for GC)
- Create `elastic_ips` DB row, state=`allocated`
- Return the public IP

**`associate_eip(eip_row, host) → None`**
- Look up host's primary ENI via `ec2.describe_instances`
- `ec2.assign_private_ip_addresses(NetworkInterfaceId, SecondaryPrivateIpAddressCount=1)`
- `ec2.associate_address(AllocationId, PrivateIpAddress, NetworkInterfaceId)`
- SSH to host: `ip addr add <private_ip>/32 dev <primary_iface>` (detect via `ip route show default`)
- Update DB: `private_ip`, `host_id`, `association_id`, state=`associated`

**`disassociate_eip(eip_row, host) → None`**
- `ec2.disassociate_address(AssociationId)`
- `ec2.unassign_private_ip_addresses(NetworkInterfaceId, PrivateIpAddresses)`
- SSH to host: `ip addr del <private_ip>/32 dev <primary_iface>`
- Clear `private_ip`, `host_id`, `association_id` in DB, state=`allocated`

**`release_eip(eip_row) → None`**
- If associated, disassociate first
- `ec2.release_address(AllocationId)`
- Delete DB row

**`migrate_eip(eip_row, from_host, to_host) → None`**
- Disassociate from old host → associate to new host
- Used when a project moves between hosts

---

## 3. Placement Integration

### EIP capacity tracking

- On host provisioning: look up `MaxIpAddressesPerInterface` from `ec2.describe_instance_types`, store as `host.max_eips` (value - 1 for primary private IP)
- Current EIP usage computed from `elastic_ips` table: `SELECT COUNT(*) FROM elastic_ips WHERE host_id = <host_id> AND state = 'associated'`

### Placement changes

- When placing a project, count `len(topology.externalIps)` → `requested_eips`
- Host is eligible only if `max_eips - current_eip_count >= requested_eips`
- If `requested_eips == 0`, EIP capacity check is skipped (no change to current behavior)

### Host capacity display

- Frontend host cards show EIP usage alongside vCPU/RAM: "EIPs: 3/14"

---

## 4. Deploy / Undeploy Integration

### Deploy flow (new step in `deploy_project_async`)

Between "networking" and "cloud-init" steps:

1. For each external IP in `topology.externalIps[]`:
   - Check if `elastic_ips` row exists for `(project_id, canvas_eip_id)` — reuse if so (stable across redeploys)
   - If not, call `eip_service.allocate_eip()`
2. Associate all project EIPs to the target host via `eip_service.associate_eip()`
3. Write actual public IPs back into project topology so frontend can display them
4. Sync SG rules (see Section 5)

### Network script changes (`generate_setup_script`)

- New "secondary IPs" block in gateway section: `ip addr add <private_ip>/32 dev <primary_iface>` (detect via `ip route show default`) for each EIP
- DNAT rules match on destination IP: `nft add rule inet nat prerouting ip daddr <private_ip> tcp dport <ext_port> dnat to <int_ip>:<int_port>`

### Undeploy flow (`stop_project_async`)

- Disassociate EIPs from host (but do NOT release — they stay allocated for redeploy stability)
- Remove dynamic SG ingress rules
- Network teardown script removes secondary IPs from eth0

### Project deletion (`destroy_project_sync`)

- Release all EIPs for the project (disassociate + release + delete DB rows)
- Remove SG rules

### Canvas external IP removal (user removes IP from panel)

- If project has a deployed EIP for that `canvas_eip_id`, release immediately via API
- Delete DB row

---

## 5. Security Group Management

### Reconciliation approach

`eip_service.sync_security_group_rules(provider, host)`:

1. List all current SG ingress rules with `Description` starting with `troshka-pf:`
2. Build desired set from all active projects on this host — each port-forward becomes: `{protocol: tcp, port: ext_port, cidr: 0.0.0.0/0, description: "troshka-pf:<project_id>:<ext_port>"}`
3. Diff current vs desired: add missing, remove stale

Runs at: deploy, undeploy, provider-level GC.

Idempotent — safe to call multiple times. Never touches SG rules without the `troshka-pf:` prefix.

AWS SG limit: 60 inbound rules by default (requestable to 1000). Each port-forward = one rule.

---

## 6. Provider-Level Garbage Collector

### New service: `services/provider_gc_service.py`

Operates at the AWS account level. Only touches resources tagged `ManagedBy: troshka`.

### GC steps

1. **EIP orphan cleanup**
   - `ec2.describe_addresses(Filters=[{Name: "tag:ManagedBy", Values: ["troshka"]}])`
   - For each tagged EIP: check if `troshka-project-id` exists in DB
   - Project missing → release the EIP
   - DB row exists but no matching AWS resource → delete stale DB row

2. **Stale SG rule cleanup**
   - List all `troshka-pf:*` described ingress rules on the Troshka SG
   - Parse project ID from description
   - Remove rules where project no longer exists or isn't deployed

3. **Orphan secondary private IP cleanup**
   - `ec2.describe_network_interfaces` for each host's primary ENI
   - For each secondary private IP: check if it corresponds to an active `elastic_ips` row
   - Unassign orphaned secondary IPs

### Safety: tag-only filtering

The GC never does a broad `describe_*` without a tag filter. If Troshka didn't tag it, Troshka doesn't touch it. This is critical for shared AWS accounts.

### API endpoint

`POST /api/v1/providers/{provider_id}/gc`
- Returns: `{eips_released: N, sg_rules_removed: N, private_ips_cleaned: N}`
- Supports `?dry_run=true` for preview

### Cron compatibility

Standard auth (API key/token) so an OCP CronJob can `curl -X POST` with credentials.

---

## 7. Frontend Changes

### ExternalIpsPanel

- IP address field becomes **read-only** — placeholder "Assigned on first deploy"
- After deploy: shows actual EIP address
- Remove button: if EIP is allocated, calls `DELETE /api/v1/projects/{id}/eips/{canvas_eip_id}` to release AWS resource, then removes from topology
- Status indicator: empty circle (not allocated), green dot (associated), yellow dot (allocated but not associated)

### PropertiesPanel (Gateway)

- No structural changes — `extIpId` already links port-forwards to external IPs
- Port-forward display shows real EIP address instead of blank

### Host cards

- Show `EIPs: 3/14` alongside existing vCPU/RAM capacity

### Provider card

- "Clean" button (same pattern as host Clean button)
- Only visible when `provider.state === "active"`
- Shows results: "Released 2 orphan EIPs, removed 3 stale SG rules"

---

## EIP Lifecycle Summary

```
User adds external IP in canvas → placeholder (no AWS resource)
        ↓
First deploy → allocate EIP, associate to host, DNAT + SG rules
        ↓
Undeploy → disassociate from host, EIP stays allocated (stable IP)
        ↓
Redeploy → re-associate same EIP to host (same public IP)
        ↓
User removes IP from canvas → release EIP (AWS resource freed)
Project deleted → release all EIPs
```

## Constraints

- **15 secondary private IPs per ENI** (instance-type dependent) — 14 usable EIP slots on m8i.xlarge after subtracting the primary private IP
- Host's own public IP is the auto-assigned EC2 IP (not an EIP) — all EIP slots available for projects
- **AWS SG rule limit**: 60 inbound rules by default per SG
- **EIPs are migratable**: can be disassociated from one host and re-associated to another
