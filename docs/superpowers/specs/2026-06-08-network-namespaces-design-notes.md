# Network Namespace Isolation — Design Notes

## Problem

Multiple projects on the same host with identical CIDRs (e.g., both use `10.0.0.0/24`) can't be isolated with routing alone. The kernel's routing table can't distinguish between two bridges with the same subnet. DNAT sends traffic to the wrong bridge, and dnsmasq can't bind to the same gateway IP on multiple bridges.

## Requirements

- Same private IPs across projects is mandatory (lab/demo documentation consistency)
- Projects must be fully isolated — no network cross-talk
- EIPs, port forwarding, outbound NAT must all work per-project
- VXLAN mesh between hosts must still work

## Architecture

Each project gets its own Linux network namespace containing all its network infrastructure:

```
Host Namespace                    Project Namespace (troshka-{pid[:8]})
┌─────────────────────┐          ┌─────────────────────────────────────┐
│ enp39s0 (public)    │          │ br-{vni} (10.0.0.1/24)             │
│                     │          │   ├─ vxlan-{vni} (from host)       │
│ veth-{pid[:8]}-host ├──────────┤ veth-{pid[:8]}-ns                  │
│                     │          │   ├─ vnet0 (VM tap, moved by hook) │
│ nftables:           │          │   ├─ vnet1 (VM tap, moved by hook) │
│   DNAT → veth IP    │          │                                     │
│   masquerade out    │          │ dnsmasq (DHCP/DNS for br-{vni})    │
│                     │          │ nftables (forward rules)            │
│ EIP secondary IPs   │          │ ip_forward = 1                     │
└─────────────────────┘          └─────────────────────────────────────┘
```

### Networking flow

**Outbound (VM → internet):**
1. VM sends packet via bridge in namespace
2. Namespace routes via veth pair to host namespace
3. Host masquerades and sends out public interface

**Inbound (EIP → VM):**
1. Packet arrives at EIP secondary private IP
2. Host nftables DNATs to veth-host IP
3. Packet enters namespace via veth pair
4. Namespace nftables forwards to VM on bridge

**DHCP:**
- dnsmasq runs inside namespace, binds to bridge — no conflicts with other projects

### VM tap device handling

libvirt creates tap devices in the default namespace. A qemu hook script moves them:

```bash
#!/bin/bash
# /etc/libvirt/hooks/qemu
# Called with: $1=domain $2=action $3=subaction
DOMAIN=$1
ACTION=$2

if [ "$ACTION" = "started" ]; then
    # Parse project namespace from domain name: troshka-{pid[:8]}-{vmid[:8]}
    NS=$(echo $DOMAIN | sed 's/troshka-\([^-]*\)-.*/troshka-\1/')
    # Find tap devices for this domain
    for tap in $(virsh domiflist $DOMAIN | awk '/tap/ {print $1}'); do
        ip link set $tap netns $NS
        ip netns exec $NS ip link set $tap master br-<vni>
        ip netns exec $NS ip link set $tap up
    done
fi
```

### Key changes from current code

- `generate_setup_script()` → wrap all bridge/dnsmasq/nftables commands in `ip netns exec`
- `generate_teardown_script()` → delete the namespace (`ip netns del`)
- Add veth pair creation/cleanup
- Deploy qemu hook script to hosts
- EIP DNAT targets veth-host IP instead of VM IP directly
- Masquerade moves from namespace to host (or stays in namespace with veth routing)

### Open questions

1. Should the VXLAN interface be in the host namespace or the project namespace?
   - Host: simpler VXLAN mesh management, veth pair bridges traffic
   - Project: cleaner isolation, but VXLAN peer management needs namespace awareness

2. How does the metadata service (169.254.169.254) work inside a namespace?

3. Performance impact of veth pairs vs direct bridge attachment?
