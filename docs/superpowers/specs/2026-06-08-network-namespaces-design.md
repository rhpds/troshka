# Network Namespace Isolation Design

## Overview

Each project gets its own Linux network namespace for complete network isolation. Multiple projects with identical CIDRs (e.g., `10.0.0.0/24`) can coexist on the same host without interference. This replaces the current approach of per-project nftables chains and policy routing, which fails when CIDRs overlap.

## Why

Same private IPs across projects is a hard requirement — lab/demo documentation and application configuration must be identical across deployments. The current approach (shared kernel routing table with policy routing) cannot route inbound DNAT to the correct bridge when two bridges share the same subnet.

## Architecture

```
Host Namespace                     Project Namespace (troshka-{pid[:8]})
┌───────────────────────┐         ┌──────────────────────────────────────┐
│ enp39s0 (public)      │         │                                      │
│ EIP secondary IPs     │         │ vxlan-{vni} ──┐                     │
│                       │         │                ├── br-{vni}          │
│ veth-{pid[:8]}-h ─────┼─────────┤ veth-{pid[:8]}-ns  (10.0.0.1/24)  │
│ (172.30.x.1/30)       │         │ (172.30.x.2/30)│                    │
│                       │         │                ├── vnet0 (VM tap)   │
│ nftables:             │         │                ├── vnet1 (VM tap)   │
│   EIP DNAT →          │         │                                      │
│   172.30.x.2          │         │ dnsmasq (DHCP/DNS)                  │
│                       │         │ nftables (masquerade, port forward) │
│                       │         │ ip_forward = 1                      │
│                       │         │ default route → 172.30.x.1          │
└───────────────────────┘         └──────────────────────────────────────┘
```

### Key decisions

- **VXLAN** moved into the project namespace (simpler — no host-side bridge needed)
- **Veth pair** connects host ↔ namespace with a unique transit subnet per project
- **VM tap devices** moved into namespace by a libvirt qemu hook script
- **Masquerade** happens inside the namespace (outbound NAT)
- **EIP DNAT** happens in the host namespace, targeting the transit IP. A second DNAT inside the namespace forwards to the VM.

---

## Transit Subnet Allocation

Each project gets a unique `/30` transit subnet for its veth pair. Derived from the VNI to avoid allocation:

```
Octet 3: (vni >> 2) & 0xFF
Octet 4 base: (vni & 0x03) * 4
172.30.{octet3}.{octet4_base}/30
Host end: {octet4_base + 1}
Namespace end: {octet4_base + 2}
```

VNIs are globally unique (1000+), so transit subnets are guaranteed unique. Supports up to 65536 VNIs in the `172.30.0.0/16` space.

---

## Namespace Lifecycle

### Create (on deploy/start)

```bash
# 1. Create namespace
ip netns add troshka-{pid[:8]}
ip netns exec troshka-{pid[:8]} ip link set lo up

# 2. Create veth pair
ip link add veth-{pid[:8]}-h type veth peer name veth-{pid[:8]}-ns
ip link set veth-{pid[:8]}-ns netns troshka-{pid[:8]}

# 3. Configure transit IPs
ip addr add 172.30.x.1/30 dev veth-{pid[:8]}-h
ip link set veth-{pid[:8]}-h up
ip netns exec troshka-{pid[:8]} ip addr add 172.30.x.2/30 dev veth-{pid[:8]}-ns
ip netns exec troshka-{pid[:8]} ip link set veth-{pid[:8]}-ns up

# 4. Create VXLAN in host, move into namespace
ip link add vxlan-{vni} type vxlan id {vni} local {host_ip} dstport 4789 nolearning
# Add VXLAN peers (head-end replication)
bridge fdb append 00:00:00:00:00:00 dev vxlan-{vni} dst {peer_ip}
ip link set vxlan-{vni} netns troshka-{pid[:8]}

# 5. Create bridge inside namespace, attach VXLAN
ip netns exec troshka-{pid[:8]} ip link add br-{vni} type bridge
ip netns exec troshka-{pid[:8]} ip link set vxlan-{vni} master br-{vni}
ip netns exec troshka-{pid[:8]} ip addr add {gateway_ip}/{prefix} dev br-{vni}
ip netns exec troshka-{pid[:8]} ip link set vxlan-{vni} up
ip netns exec troshka-{pid[:8]} ip link set br-{vni} up

# 6. Routing inside namespace
ip netns exec troshka-{pid[:8]} sysctl -w net.ipv4.ip_forward=1
ip netns exec troshka-{pid[:8]} ip route add default via 172.30.x.1

# 7. Host route to transit
ip route add 172.30.x.0/30 dev veth-{pid[:8]}-h

# 8. dnsmasq inside namespace
ip netns exec troshka-{pid[:8]} dnsmasq --conf-file=/etc/dnsmasq.d/troshka-{vni}.conf

# 9. nftables inside namespace
ip netns exec troshka-{pid[:8]} nft add table inet nat
ip netns exec troshka-{pid[:8]} nft add chain inet nat postrouting '{ type nat hook postrouting priority 100; }'
ip netns exec troshka-{pid[:8]} nft add rule inet nat postrouting oifname "veth-{pid[:8]}-ns" masquerade
# Port forward DNAT (inside namespace)
ip netns exec troshka-{pid[:8]} nft add chain inet nat prerouting '{ type nat hook prerouting priority -100; }'
ip netns exec troshka-{pid[:8]} nft add rule inet nat prerouting tcp dport {ext_port} dnat ip to {int_ip}:{int_port}
```

### Destroy (on stop/delete)

```bash
ip netns del troshka-{pid[:8]}    # Kills everything inside: dnsmasq, nftables, bridges, routes
ip link del veth-{pid[:8]}-h 2>/dev/null || true   # Host-side veth (may already be gone)
ip route del 172.30.x.0/30 2>/dev/null || true
```

One command (`ip netns del`) replaces the current 10+ cleanup commands.

---

## Qemu Hook Script

Deployed to `/etc/libvirt/hooks/qemu` on each host during agent install.

```bash
#!/bin/bash
DOMAIN=$1
ACTION=$2

if [ "$ACTION" = "started" ]; then
    PID=$(echo "$DOMAIN" | sed -n 's/^troshka-\([a-f0-9]*\)-.*/\1/p')
    [ -z "$PID" ] && exit 0
    NS="troshka-$PID"
    ip netns list | grep -q "^$NS " || exit 0

    BRIDGE=$(ip netns exec "$NS" ip -o link show type bridge 2>/dev/null | awk -F': ' '{print $2}' | head -1)
    [ -z "$BRIDGE" ] && exit 0

    for TAP in $(virsh domiflist "$DOMAIN" 2>/dev/null | awk '/^[a-z]/ && !/^Name/ {print $1}'); do
        ip link set "$TAP" netns "$NS" 2>/dev/null
        ip netns exec "$NS" ip link set "$TAP" master "$BRIDGE" 2>/dev/null
        ip netns exec "$NS" ip link set "$TAP" up 2>/dev/null
    done
fi
```

Deployed once. Idempotent. Only acts on `troshka-*` domains.

---

## EIP DNAT (Host Namespace)

Host nftables only handles EIP→transit forwarding:

```bash
# Per-EIP port forward: EIP private IP → namespace transit IP
nft add rule inet nat prerouting ip daddr {eip_private_ip} tcp dport {ext_port} dnat ip to 172.30.x.2:{ext_port}
```

Inside the namespace, the prerouting chain forwards to the VM:
```bash
nft add rule inet nat prerouting tcp dport {ext_port} dnat ip to {int_ip}:{int_port}
```

Host nftables also needs to allow forwarding through the veth:
```bash
# Host forward chain (needed for masquerade return traffic)
nft add rule inet filter forward iifname "veth-{pid[:8]}-h" accept
nft add rule inet filter forward oifname "veth-{pid[:8]}-h" accept
```

---

## Traffic Flows

### Outbound (VM → internet)
1. VM sends packet (src `10.0.0.20`, dst `8.8.8.8`)
2. Bridge in namespace forwards to default route → `172.30.x.1`
3. Namespace nftables masquerades: src becomes `172.30.x.2`
4. Packet crosses veth to host namespace
5. Host routes to internet via `enp39s0`, NATs source to public IP

### Inbound (EIP → VM)
1. Packet arrives at EIP secondary private IP (`10.100.1.x`)
2. Host nftables DNATs to `172.30.x.2:{ext_port}`
3. Packet crosses veth into namespace
4. Namespace nftables DNATs to `{int_ip}:{int_port}` (e.g., `10.0.0.20:80`)
5. Bridge delivers to VM

### DHCP
- dnsmasq runs inside namespace on the bridge — no conflicts with other namespaces
- Identical gateway IPs (`10.0.0.1`) on different namespaces are fine

### Inter-VM (same project)
- Bridge handles L2 switching — stays inside the namespace
- No routing needed

---

## What Changes

### Modified files
- `src/backend/app/services/vxlan.py` — rewrite `generate_setup_script()` to create namespace, veth, move VXLAN, wrap all commands in `ip netns exec`
- `src/backend/app/services/deploy_service.py` — teardown/destroy use `ip netns del`, remove policy routing cleanup, remove per-project chain cleanup
- `src/backend/app/services/provisioner.py` — deploy qemu hook script in cloud-init
- `src/backend/app/services/agent_deployer.py` — deploy qemu hook script as fallback
- `src/backend/app/services/gc_service.py` — clean orphaned namespaces (find `troshka-*` namespaces with no matching project)

### Removed
- Per-project nftables chains in host namespace (`troshka-fwd/post/pre-*`)
- Policy routing (ip rule/ip route table)
- `bind-dynamic` dnsmasq workaround
- Main-table route conflict handling

### Unchanged
- EIP service (allocate/associate/disassociate/release)
- SG management
- Placement
- Frontend
- `build_host_network_config()` — same config dict, different script output
- VM lifecycle commands (virsh start/stop/destroy)
- Per-VM start (namespace is already up, hook moves tap)

---

## GC Changes

Host-level GC adds namespace orphan detection:

```bash
# List troshka namespaces
ip netns list | grep '^troshka-'
```

For each namespace, check if the project ID prefix matches an active project. Orphaned namespaces get deleted.

---

## Constraints

- **Transit subnet space**: `172.30.0.0/16` supports ~16k projects (VNI range 1000-16M mapped to /30 subnets)
- **Qemu hook**: must be deployed before VMs can start in namespaces. Existing VMs need redeployment.
- **Migration**: existing projects need a stop/start cycle to transition from old networking to namespaced networking. No in-place migration.
