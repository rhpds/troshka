# OCP Virt Route-Based External Access

**Date**: 2026-06-24
**Status**: Draft
**Goal**: On OCP Virt, use OCP Routes instead of NodePort/EIP for external access to project VMs — seamless with public cloud providers that use EIPs.

## Background

Troshka projects can expose internal VMs externally via the gateway's `portForwards` config. Currently:

- **AWS/GCP/Azure**: Each VM gets an EIP. Port forwards use nftables DNAT rules on the host. Multiple VMs with port 443 just work — each has its own public IP.
- **OCP Virt**: External access uses NodePort Services. This consumes high-numbered ports (30000+), requires the student to know the NodePort, and can't do port 443 directly.

OCP Routes solve this cleanly — TLS passthrough or edge termination on port 443, SNI-based routing, unlimited endpoints, proper hostnames.

## Design

### Template Format (unchanged)

Templates continue to use the same `gateway.port_forwards` syntax:

```yaml
gateway:
  network: cluster
  external_access: true
  port_forwards:
    - ext_port: 443
      int_ip: 10.0.0.50
      int_port: 443
      proto: tcp
```

The template is provider-agnostic. The provider driver decides how to implement the external access.

### Provider Behavior

#### AWS/GCP/Azure (unchanged)
1. Allocate EIP per VM with external access
2. Create nftables DNAT rules for port forwards
3. Return public IP as the external endpoint

#### OCP Virt (new)
1. For each port forward targeting port 443 or 80:
   - Create a `ClusterIP` Service pointing to the VM's pod on the target port
   - Create an OCP Route with hostname `{vm_name}-{project_id[:8]}.apps.{cluster_domain}`
   - Route type: TLS passthrough (for 443) or edge (for 80)
2. For other ports (22, 6443, 8080, etc.):
   - Fall back to NodePort Service (current behavior)
3. Return the Route hostname as the external endpoint

### Route Naming Convention

```
{vm_name}-{project_id[:8]}.apps.{cluster_domain}
```

Examples:
- `bastion-a53cbd0d.apps.ocpvdev01.dal13.infra.demo.redhat.com` → bastion:443 (showroom)
- `hub-a53cbd0d.apps.ocpvdev01.dal13.infra.demo.redhat.com` → hub:6443 (API — if routed)

This follows the same pattern as the console Routes (`{instance_id}.{console_domain}`).

### How the Deploy Service Uses It

The deploy service calls `provider.create_external_access(project, topology)` which returns a list of endpoints. Currently it returns EIP addresses. With this change:

- On public cloud: returns `{"type": "eip", "ip": "1.2.3.4", "vm_id": "...", "ports": [443]}`
- On OCP Virt: returns `{"type": "route", "hostname": "bastion-a53cbd0d.apps...", "vm_id": "...", "ports": [443]}`

The topology JSONB stores the external endpoint (IP or hostname) so the frontend and agnosticd can reference it.

### Showroom Integration

The showroom role needs the bastion's public hostname for:
1. Traefik TLS certificate (Let's Encrypt or ZeroSSL)
2. The wetty terminal URL
3. Lab content URLs

With OCP Routes, TLS is handled by the OCP router (edge termination) — showroom's Traefik doesn't need its own cert. The Route hostname becomes the `bastion_public_hostname` that showroom uses.

For the IBI lab:
- OCP Virt: `bastion-{pid}.apps.ocpvdev01...` → Route → bastion:443 (Traefik)
- AWS: `{eip}` → nftables → bastion:443 (Traefik with Let's Encrypt)

### End-to-End Data Flow

The external endpoint (Route hostname or EIP IP) must flow from Troshka → topology JSONB → Troshka API → agnosticd inventory → showroom config. Here's how:

#### Step 1: Troshka stores the external endpoint in topology

The deploy service stores external access info on the gateway node in the topology JSONB. Currently `externalIps` stores EIP data. Add an `externalEndpoints` list that works for both providers:

```json
{
  "externalEndpoints": [
    {
      "vmName": "bastion",
      "vmIp": "10.0.0.50",
      "port": 443,
      "type": "route",
      "hostname": "bastion-a53cbd0d.apps.ocpvdev01.dal13..."
    }
  ]
}
```

On public cloud:
```json
{
  "externalEndpoints": [
    {
      "vmName": "bastion",
      "vmIp": "10.0.0.50",
      "port": 443,
      "type": "eip",
      "ip": "34.56.78.90"
    }
  ]
}
```

#### Step 2: Troshka API exposes the endpoint

The `project_info` module already returns the full topology. No API change needed — `externalEndpoints` is part of the gateway node data.

#### Step 3: agnosticd inventory builder sets `public_dns_name`

In `infrastructure_deployment.yml`, when building the bastion inventory, look up the external endpoint from the topology and set it as a host var:

```yaml
- name: Find bastion external endpoint
  ansible.builtin.set_fact:
    _bastion_external: >-
      {{ _project.topology.nodes
         | selectattr('data.subtype', 'defined')
         | selectattr('data.subtype', 'equalto', 'gateway')
         | map(attribute='data.externalEndpoints')
         | first | default([])
         | selectattr('vmName', 'equalto', _bastion_vm.data.name)
         | first | default({}) }}

- name: Add bastion to inventory
  ansible.builtin.add_host:
    name: "{{ _bastion_vm.data.name }}"
    public_dns_name: "{{ _bastion_external.hostname | default(_bastion_external.ip | default('')) }}"
    ...
```

This sets `public_dns_name` to the Route hostname on OCP Virt, or the EIP IP on public cloud. The host var is provider-agnostic.

#### Step 4: Catalog item uses the host var

In the catalog item's `common.yaml`:

```yaml
showroom_host: "{{ hostvars[groups['bastions'][0]]['public_dns_name'] }}"
```

This works identically on both providers — the value is whatever the inventory builder set.

#### TLS Handling

- **OCP Virt**: OCP router handles TLS termination. Set `showroom_tls_provider: none` — Traefik inside the bastion doesn't need its own cert since traffic arrives already TLS-terminated (or passthrough).
- **Public cloud**: Showroom's Traefik uses Let's Encrypt/ZeroSSL as usual with the EIP's DNS name.

The catalog item can conditionally set `showroom_tls_provider` based on the provider type, or Troshka can include the provider type in the topology data for agnosticd to reference.

## Implementation

### Files Modified

**Troshka backend:**

- **`src/backend/app/services/providers/ocpvirt.py`**:
  - Add `_create_route()` — creates ClusterIP Service + OCP Route for port 443/80 forwards
  - Add `_delete_route()` — cleanup on project destroy
  - Store Route hostname in topology `externalEndpoints`

- **`src/backend/app/services/providers/ec2.py`** (and gcp.py, azure.py):
  - Store EIP IP in topology `externalEndpoints` (refactor from current `externalIps` format)

- **`src/backend/app/services/deploy_service.py`**:
  - Write `externalEndpoints` to gateway node data after external access setup

**agnosticd-v2:**

- **`ansible/cloud_providers/troshka/infrastructure_deployment.yml`**:
  - Read `externalEndpoints` from topology gateway node
  - Set `public_dns_name` on bastion host var (hostname or IP)

**infra/ocpvirt-rbac.yaml** — no change (Routes RBAC already exists)

### RBAC (already in place)

```yaml
- apiGroups: ["route.openshift.io"]
  resources: ["routes"]
  verbs: ["create", "delete", "get", "list"]
- apiGroups: [""]
  resources: ["services"]
  verbs: ["create", "delete", "get", "list", "patch"]
```

## Non-Goals

- Custom domain names for Routes (use the apps wildcard domain)
- Changing the template format (stays provider-agnostic)
- Changing AWS/GCP/Azure EIP allocation behavior (only adding `externalEndpoints` data)

## Verification

1. Deploy IBI lab on OCP Virt — verify Route created, showroom accessible via Route hostname
2. Deploy same template on AWS — verify EIP allocated, showroom accessible via EIP
3. Verify `public_dns_name` is set correctly in agnosticd inventory for both providers
4. Verify `showroom_host` resolves to the correct endpoint in both cases
5. Verify Route/Service cleanup on project destroy
