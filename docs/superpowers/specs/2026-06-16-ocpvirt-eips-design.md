# OCP Virt EIPs via MetalLB — Design Spec

**Date:** 2026-06-16
**Status:** Approved
**Prereq:** OCP Virt Phase 2 complete (host provisioning, deploy working on ocpvdev01)

---

## Goal

External IP allocation for nested VMs on OCP Virt hosts, same UX as EC2 EIPs. Users can allocate multiple EIPs per project, assign port forwards to different VMs, and have two web servers on port 443 with separate public IPs — all through the existing canvas interface.

## Background

### EC2 EIP Flow (current)

1. `eip_service.allocate_eip()` → AWS `allocate_address()` → public IP + allocation ID
2. `eip_service.associate_eip()` → assign secondary private IP on host ENI, map EIP to that private IP
3. troshkad host nftables: `ip daddr <secondary-private-ip> tcp dport <ext-port> → dnat to <transit-ip>:<ext-port>`
4. troshkad namespace nftables: `ip daddr <transit-ip> tcp dport <ext-port> → dnat to <nested-vm-ip>:<int-port>`
5. Key discriminator: each EIP has a unique **private IP** on the host

### OCP Virt Problem

KubeVirt masquerade networking gives the host VM a single pod IP. All inbound traffic arrives at the same address regardless of which MetalLB Service sent it. Two EIPs both exposing port 443 are indistinguishable by destination IP.

## Design

### Transit Port Scheme

Each port forward gets a unique "transit port" from range 40000-49999, allocated per host. The MetalLB LoadBalancer Service maps user-facing ports to transit ports. troshkad DNATs based on port number instead of destination IP.

**OCP Virt EIP flow:**
```
Internet → MetalLB IP:443
  → LB Service maps port 443 → targetPort 40001
  → kube-proxy → pod:40001
  → KubeVirt masquerade → VM eth0:40001
  → troshkad host nftables: dport 40001 → dnat to 172.30.X.10:40001
  → troshkad namespace nftables: dport 40001 → dnat to 192.168.1.50:443
```

**EC2 flow (unchanged):**
```
Internet → EIP public IP:443
  → AWS maps to secondary private IP 10.0.1.55:443
  → troshkad host nftables: daddr 10.0.1.55 dport 443 → dnat to 172.30.X.10:443
  → troshkad namespace nftables: daddr 172.30.X.10 dport 443 → dnat to 192.168.1.50:443
```

### Provider Driver Modularity

All cloud-specific API calls go through the `ProviderDriver` interface. Service-layer modules (`eip_service`, `deploy_service`) never import cloud SDKs directly. This ensures adding Azure/GCP providers later requires only a new driver class.

### Data Model Changes

**Provider model** — new column:
- `max_eips: int | None` — optional cap on total EIP allocations for this provider. Null = unlimited (bounded by MetalLB pool size). Checked at allocation time.

**ElasticIp model** — generalize + new column:
- `allocation_id` — AWS allocation ID (EC2) or LB Service name (OCP Virt, e.g. `troshka-eip-{eip_id[:8]}`)
- `association_id` — AWS association ID (EC2) or null (OCP Virt)
- `private_ip` — secondary ENI IP (EC2) or null (OCP Virt — transit ports replace this)
- **New:** `port_map: JSONB | None` — transit port mappings for OCP Virt: `{"443": 40001, "8080": 40002}` (ext_port string → transit_port int). Null for EC2.

Transit port allocation: scan existing `port_map` values across all EIPs on the same host, assign next available port starting from 40000.

### Provider Driver Interface

New methods on `ProviderDriver` (`base.py`):

```python
def allocate_eip(self, provider, host, eip_id):
    """Allocate an external IP. Returns {public_ip, allocation_id}."""
    raise NotImplementedError

def associate_eip(self, provider, host, allocation_id):
    """Associate an EIP with a host. Returns {private_ip, association_id} or {}."""
    raise NotImplementedError

def release_eip(self, provider, allocation_id, namespace=None):
    """Release an external IP and clean up infra resources.
    namespace is provider-specific context (k8s namespace for OCP Virt, ignored for EC2)."""
    raise NotImplementedError

def update_eip_ports(self, provider, host, allocation_id, ports):
    """Update port mappings on an EIP. ports = [{port, targetPort, name}].
    No-op for providers that don't need it (EC2)."""
    pass
```

**EC2Driver implementation:**
- `allocate_eip()`: `ec2.allocate_address(Domain="vpc")`, tag, return public IP + allocation ID
- `associate_eip()`: `ec2.assign_private_ip_addresses()` + `ec2.associate_address()` → return private_ip + association_id
- `release_eip()`: `ec2.disassociate_address()` + `ec2.unassign_private_ip_addresses()` if association exists, then `ec2.release_address()`. Reads association state from DB (passed via allocation_id lookup).
- `update_eip_ports()`: no-op

**OCPVirtDriver implementation:**
- `allocate_eip()`: create LoadBalancer Service `troshka-eip-{eip_id[:8]}` with selector `kubevirt.io/domain: {hostname}`, no ports initially. Poll for MetalLB IP. Return MetalLB IP + service name.
- `associate_eip()`: no-op, return empty dict (LB Service already targets the pod)
- `release_eip()`: delete the LB Service
- `update_eip_ports()`: patch Service `.spec.ports` — for each port forward, `{port: ext_port, targetPort: transit_port, name: "pf-{idx}"}`

### eip_service.py Refactoring

Remove all boto3/cloud SDK imports. Dispatch through `get_provider_driver(provider)`.

```python
def allocate_eip(db, provider, project_id, canvas_eip_id, host):
    driver = get_provider_driver(provider)
    result = driver.allocate_eip(provider, host, eip_id)
    # Create ElasticIp DB row
    eip = ElasticIp(
        provider_id=provider.id, project_id=project_id,
        canvas_eip_id=canvas_eip_id,
        allocation_id=result["allocation_id"],
        public_ip=result["public_ip"], state="allocated",
    )
    db.add(eip); db.commit()
    return eip

def associate_eip(db, eip, host):
    driver = get_provider_driver(provider)
    result = driver.associate_eip(provider, host, eip.allocation_id)
    eip.private_ip = result.get("private_ip")       # EC2 only
    eip.association_id = result.get("association_id") # EC2 only
    eip.host_id = host.id
    eip.state = "associated"
    db.commit()

def release_eip(db, eip):
    provider = db.query(Provider).filter_by(id=eip.provider_id).first()
    driver = get_provider_driver(provider)
    # OCP Virt needs namespace from provider creds; EC2 ignores it
    ns = provider.get_credentials().get("namespace") if provider.type == "ocpvirt" else None
    driver.release_eip(provider, eip.allocation_id, namespace=ns)
    db.delete(eip); db.commit()

def sync_security_group_rules(db, provider, desired_rules):
    if provider.type != "ec2":
        return {"added": 0, "removed": 0}  # MetalLB/Azure LB handle exposure
    # ... existing EC2 SG logic unchanged ...
```

Transit port allocation (OCP Virt only, called during deploy after port forwards are known):

```python
def allocate_transit_ports(db, eip, host, port_forwards):
    """Allocate transit ports for OCP Virt EIP port forwards."""
    # Collect used transit ports on this host
    used = set()
    for other in db.query(ElasticIp).filter_by(host_id=host.id):
        if other.port_map:
            used.update(other.port_map.values())
    # Assign next available
    port_map = {}
    next_port = 40000
    for pf in port_forwards:
        while next_port in used:
            next_port += 1
        port_map[str(pf["extPort"])] = next_port
        used.add(next_port)
        next_port += 1
    eip.port_map = port_map
    db.commit()
    return port_map
```

### deploy_service.py Changes

Minimal changes to the deploy pipeline:

**Step 0 — EIP allocation (~line 1282):**
- `allocate_eip()` now takes `host` parameter
- After association, if provider is OCP Virt: call `allocate_transit_ports()`, then `driver.update_eip_ports()` to patch the LB Service
- Set `ext_ip["_transit_port_map"] = eip.port_map` when port_map is not null

**Port forward data (~line 1326):**
- Existing: `ext_ip["_private_ip"] = eip.private_ip`
- Added: `ext_ip["_transit_port_map"] = eip.port_map`
- `vxlan.py` propagates `_transit_port` per port forward entry from the map

**Security group sync (~line 1387):**
- `sync_security_group_rules()` no-ops for non-EC2

**Undeploy (~line 2747):**
- `release_eip()` unchanged — driver handles LB Service cleanup

**Placement (~line 124):**
- Existing host-level check: `host.max_eips - eip_used < required_eips`
- Added: provider-level check if `provider.max_eips` is set

### troshkad Changes

The network setup handler receives port forward data with either `_private_ip` (EC2) or `_transit_port` (OCP Virt). troshkad is provider-agnostic — it uses whichever field is present.

**Host-level DNAT (~line 2954):**
```python
# Current (EC2 path — unchanged):
if priv_ip:
    # nft add rule ... ip daddr <priv_ip> tcp dport <ext_port> dnat to <transit_ip>:<ext_port>

# New (OCP Virt path — added):
transit_port = pf.get("_transit_port")
if transit_port:
    # nft add rule ... tcp dport <transit_port> dnat to <pf_transit_ip>:<transit_port>
```

**Namespace-level DNAT (~line 2782):**
```python
# Current: daddr <pf_transit_ip> tcp dport <ext_port> dnat to <int_ip>:<int_port>
# Updated: use transit_port if present, else ext_port
effective_port = pf.get("_transit_port") or ext_port
# nft add rule ... ip daddr <pf_transit_ip> tcp dport <effective_port> dnat to <int_ip>:<int_port>
```

### KubeVirt VM Spec Change

Remove explicit masquerade ports from `ocpvirt.py` (`provision_host()`):

**Before:**
```python
"interfaces": [{
    "masquerade": {},
    "name": "default",
    "ports": [{"port": 22}, {"port": 31337}, {"port": 443}],
}]
```

**After:**
```python
"interfaces": [{
    "masquerade": {},
    "name": "default",
}]
```

When no ports are listed, KubeVirt forwards all ports. This is safe because:
- The LB Service only exposes specific ports externally
- Nested VMs are isolated behind VXLAN/nftables/namespace (same boundary as EC2)
- The masquerade port list only affects pod-network-to-VM traffic (already trusted)

Only affects newly provisioned hosts. Existing hosts need reprovisioning.

### Host max_eips

`provision_host()` return value changes from `"max_eips": 0` to `"max_eips": 100` for OCP Virt hosts. The real constraint is the provider-level MetalLB pool, but per-host cap prevents one host from consuming the entire pool.

### Cleanup

**`terminate_host()`:** Add cleanup for `troshka-eip-*` LB Services. Use label selector `troshka/host-id={host_id}` to find all services associated with a host.

**Undeploy:** `release_eip()` through driver deletes the LB Service.

## Files Modified

1. `src/backend/app/services/providers/base.py` — add `allocate_eip`, `associate_eip`, `release_eip`, `update_eip_ports`
2. `src/backend/app/services/providers/ec2.py` — implement EIP driver methods (extract from eip_service)
3. `src/backend/app/services/providers/ocpvirt.py` — implement EIP methods (LB CRUD), remove masquerade ports, bump max_eips
4. `src/backend/app/services/eip_service.py` — refactor to dispatch through driver, add transit port allocation, remove boto3 imports
5. `src/backend/app/services/deploy_service.py` — pass host to allocate_eip, propagate transit port map
6. `src/backend/app/services/vxlan.py` — pass `_transit_port` through to gateway port forward config
7. `src/troshkad/troshkad.py` — add `_transit_port` branch to host-level and namespace-level DNAT rules
8. `src/backend/app/models/elastic_ip.py` — add `port_map` JSONB column
9. `src/backend/app/models/provider.py` — add `max_eips` column
10. `src/backend/app/services/placement.py` — provider-level EIP cap check
11. Alembic migration for `port_map` and `max_eips` columns

## Files NOT Modified

- **Frontend** — external access toggle already exists, gated by host `max_eips` (which moves from 0 to 100)
- **Canvas/topology model** — `externalIps` JSONB structure unchanged
- **troshkad namespace nftables** — transit port flows through existing DNAT path

## Testing

1. Deploy a project on OCP Virt with external access enabled (1 EIP, 1 port forward)
2. Verify MetalLB assigns IP, LB Service has correct port mapping, nftables rules work
3. Deploy with 2 EIPs both on port 443 → verify different MetalLB IPs, different transit ports, both reachable
4. Deploy with 1 EIP, multiple port forwards (port X → VM-A, port Y → VM-B)
5. Undeploy → verify LB Services cleaned up
6. Verify EC2 EIP flow still works unchanged (regression)
7. Test provider-level max_eips cap (set low, try to exceed)
