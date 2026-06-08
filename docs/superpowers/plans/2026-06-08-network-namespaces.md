# Network Namespace Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Isolate each project's networking in its own Linux network namespace so multiple projects with identical CIDRs can coexist on the same host.

**Architecture:** Each project gets a namespace (`troshka-{pid[:8]}`) containing its bridges, VXLAN, dnsmasq, and nftables. A veth pair with a unique transit subnet connects it to the host namespace. EIP DNAT happens in the host namespace targeting the transit IP; port-forward DNAT and masquerade happen inside the namespace. A qemu hook script moves VM tap devices into the namespace on VM start.

**Tech Stack:** Python 3.11, bash, Linux network namespaces, nftables, dnsmasq, libvirt hooks

**Spec:** `docs/superpowers/specs/2026-06-08-network-namespaces-design.md`

---

## File Structure

### New files
- None — all changes are modifications to existing files

### Modified files
- `src/backend/app/services/vxlan.py` — rewrite `generate_setup_script()` for namespaced networking
- `src/backend/app/services/deploy_service.py` — simplify teardown (namespace deletion), update all script generators
- `src/backend/app/services/provisioner.py` — deploy qemu hook in cloud-init
- `src/backend/app/services/agent_deployer.py` — deploy qemu hook in agent install
- `src/backend/app/services/gc_service.py` — add namespace orphan detection
- `src/backend/app/api/eips.py` — update EIP sync to use transit IPs for host DNAT

---

### Task 1: Deploy Qemu Hook Script

**Files:**
- Modify: `src/backend/app/services/provisioner.py` (cloud-init runcmd)
- Modify: `src/backend/app/services/agent_deployer.py` (AGENT_INSTALL_SCRIPT)

- [ ] **Step 1: Add qemu hook to cloud-init**

In `src/backend/app/services/provisioner.py`, add to the `runcmd` section of `CLOUD_INIT` (after the `mkdir` line):

```python
  - |
    mkdir -p /etc/libvirt/hooks
    cat > /etc/libvirt/hooks/qemu << 'HOOKEOF'
    #!/bin/bash
    DOMAIN=$1
    ACTION=$2
    if [ "$ACTION" = "started" ]; then
        PID=$(echo "$DOMAIN" | sed -n 's/^troshka-\([a-f0-9]*\)-.*/\1/p')
        [ -z "$PID" ] && exit 0
        NS="troshka-$PID"
        ip netns list 2>/dev/null | grep -q "^$NS " || exit 0
        BRIDGE=$(ip netns exec "$NS" ip -o link show type bridge 2>/dev/null | awk -F': ' '{print $2}' | head -1)
        [ -z "$BRIDGE" ] && exit 0
        for TAP in $(virsh domiflist "$DOMAIN" 2>/dev/null | awk '/^[a-z]/ && !/^Name/ {print $1}'); do
            ip link set "$TAP" netns "$NS" 2>/dev/null
            ip netns exec "$NS" ip link set "$TAP" master "$BRIDGE" 2>/dev/null
            ip netns exec "$NS" ip link set "$TAP" up 2>/dev/null
        done
    fi
    HOOKEOF
    chmod +x /etc/libvirt/hooks/qemu
```

- [ ] **Step 2: Add qemu hook to agent install script**

In `src/backend/app/services/agent_deployer.py`, add the same hook deployment to the `AGENT_INSTALL_SCRIPT` after the polkit setup section. Same bash content as above.

- [ ] **Step 3: Run tests**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: all tests pass (no behavioral changes yet).

- [ ] **Step 4: Commit**

```bash
git add src/backend/app/services/provisioner.py src/backend/app/services/agent_deployer.py
git commit -m "feat: deploy libvirt qemu hook script for namespace tap device migration"
```

---

### Task 2: Add Transit Subnet Helper

**Files:**
- Modify: `src/backend/app/services/vxlan.py` (add helper function)

- [ ] **Step 1: Add transit subnet calculation function**

Add to `src/backend/app/services/vxlan.py` after the `VNI_MAX` constant:

```python
def _transit_subnet(vni: int) -> dict:
    """Calculate the unique /30 transit subnet for a VNI's veth pair.
    
    Returns dict with host_ip, ns_ip, and cidr for the transit link.
    Uses 172.30.0.0/16 space, derived deterministically from VNI.
    """
    octet3 = (vni >> 2) & 0xFF
    octet4_base = (vni & 0x03) * 4
    return {
        "host_ip": f"172.30.{octet3}.{octet4_base + 1}",
        "ns_ip": f"172.30.{octet3}.{octet4_base + 2}",
        "cidr": f"172.30.{octet3}.{octet4_base}/30",
    }
```

- [ ] **Step 2: Run tests**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`

- [ ] **Step 3: Commit**

```bash
git add src/backend/app/services/vxlan.py
git commit -m "feat: add transit subnet helper for namespace veth pairs"
```

---

### Task 3: Rewrite generate_setup_script for Namespaces

**Files:**
- Modify: `src/backend/app/services/vxlan.py` (rewrite `generate_setup_script`)

This is the core change. The function signature stays the same but the generated script creates a namespace instead of shared-kernel networking.

- [ ] **Step 1: Replace the AGENT_SETUP_SCRIPT template**

Replace the `AGENT_SETUP_SCRIPT` template and `generate_setup_script` function in `src/backend/app/services/vxlan.py` with:

```python
AGENT_SETUP_SCRIPT = """#!/bin/bash
# Auto-generated by troshka — sets up namespaced networking on a host
set -uo pipefail

# ── Namespace + veth setup ──
{namespace_commands}

# ── VXLAN + Bridge setup (inside namespace) ──
{vxlan_commands}

# ── DHCP (dnsmasq inside namespace) ──
{dhcp_commands}

# ── nftables (inside namespace) ──
{nft_commands}

# ── Host nftables (EIP DNAT + forwarding) ──
{host_nft_commands}

echo "Network setup complete"
"""


def generate_setup_script(config: dict, host_ip: str, project_id: str = "") -> str:
    """Generate a shell script that sets up namespaced networking on a host.

    Each project gets its own network namespace containing bridges, VXLAN,
    dnsmasq, and nftables. A veth pair connects it to the host namespace.
    """
    pid = project_id[:8] if project_id else "default"
    ns = f"troshka-{pid}"
    veth_h = f"veth-{pid}-h"
    veth_ns = f"veth-{pid}-ns"

    ns_cmds = []
    vxlan_cmds = []
    dhcp_cmds = []
    nft_cmds = []
    host_nft_cmds = []

    # Find the first VNI to derive the transit subnet
    first_vni = None
    for net in config.get("networks", []):
        first_vni = net["vni"]
        break
    if not first_vni:
        return "#!/bin/bash\necho 'No networks to configure'"

    transit = _transit_subnet(first_vni)

    # Create namespace and veth pair
    ns_cmds.append(f"ip netns del {ns} 2>/dev/null || true")
    ns_cmds.append(f"ip link del {veth_h} 2>/dev/null || true")
    ns_cmds.append(f"ip netns add {ns}")
    ns_cmds.append(f"ip netns exec {ns} ip link set lo up")
    ns_cmds.append(f"ip link add {veth_h} type veth peer name {veth_ns}")
    ns_cmds.append(f"ip link set {veth_ns} netns {ns}")
    ns_cmds.append(f"ip addr add {transit['host_ip']}/30 dev {veth_h} 2>/dev/null || true")
    ns_cmds.append(f"ip link set {veth_h} up")
    ns_cmds.append(f"ip netns exec {ns} ip addr add {transit['ns_ip']}/30 dev {veth_ns}")
    ns_cmds.append(f"ip netns exec {ns} ip link set {veth_ns} up")

    # Routing
    ns_cmds.append(f"ip netns exec {ns} sysctl -w net.ipv4.ip_forward=1")
    ns_cmds.append(f"ip netns exec {ns} ip route add default via {transit['host_ip']}")
    ns_cmds.append(f"ip route add {transit['cidr']} dev {veth_h} 2>/dev/null || true")

    # Host nftables: allow forwarding through veth + EIP DNAT + masquerade for transit
    host_nft_cmds.append("nft add table inet filter 2>/dev/null || true")
    host_nft_cmds.append("nft add chain inet filter forward '{ type filter hook forward priority 0; policy accept; }' 2>/dev/null || true")
    host_nft_cmds.append("nft add table inet nat 2>/dev/null || true")
    host_nft_cmds.append("nft add chain inet nat postrouting '{ type nat hook postrouting priority 100; }' 2>/dev/null || true")
    host_nft_cmds.append("nft add chain inet nat prerouting '{ type nat hook prerouting priority -100; }' 2>/dev/null || true")
    host_nft_cmds.append(f"nft add rule inet filter forward iifname \"{veth_h}\" accept 2>/dev/null || true")
    host_nft_cmds.append(f"nft add rule inet filter forward oifname \"{veth_h}\" accept 2>/dev/null || true")
    # Masquerade transit traffic going to internet
    host_nft_cmds.append(f"nft add rule inet nat postrouting ip saddr {transit['cidr']} masquerade 2>/dev/null || true")

    for net in config.get("networks", []):
        vni = net["vni"]
        bridge = net["bridge_name"]
        vxlan_if = net["vxlan_name"]
        cidr = net.get("cidr", "")

        # Create VXLAN in host namespace, add peers, then move into project namespace
        vxlan_cmds.append(f"ip link show {vxlan_if} &>/dev/null && ip link del {vxlan_if}")
        vxlan_cmds.append(f"ip link add {vxlan_if} type vxlan id {vni} local {host_ip} dstport 4789 nolearning")
        for peer in net.get("peers", []):
            if peer != host_ip:
                vxlan_cmds.append(f"bridge fdb append 00:00:00:00:00:00 dev {vxlan_if} dst {peer}")
        vxlan_cmds.append(f"ip link set {vxlan_if} netns {ns}")

        # Create bridge inside namespace, attach VXLAN
        vxlan_cmds.append(f"ip netns exec {ns} ip link add {bridge} type bridge 2>/dev/null || true")
        vxlan_cmds.append(f"ip netns exec {ns} ip link set {vxlan_if} master {bridge}")
        vxlan_cmds.append(f"ip netns exec {ns} ip link set {vxlan_if} up")
        vxlan_cmds.append(f"ip netns exec {ns} ip link set {bridge} up")

        # Assign bridge IP if DHCP/DNS enabled
        if net.get("dhcp_enabled") or net.get("dns_enabled"):
            gateway_ip = net.get("dhcp_config", {}).get("gateway", "")
            if gateway_ip and cidr:
                prefix = cidr.split("/")[1] if "/" in cidr else "24"
                vxlan_cmds.append(f"ip netns exec {ns} ip addr add {gateway_ip}/{prefix} dev {bridge} 2>/dev/null || true")

        # DHCP via dnsmasq inside namespace
        if net.get("dhcp_enabled"):
            dhcp_cfg = net.get("dhcp_config", {})
            range_start = dhcp_cfg.get("range_start", "")
            range_end = dhcp_cfg.get("range_end", "")
            lease = dhcp_cfg.get("lease_time", "24h")
            if range_start and range_end:
                dnsmasq_conf = f"/etc/dnsmasq.d/troshka-{vni}.conf"
                dnsmasq_pid = f"/run/troshka-dnsmasq-{vni}.pid"
                dnsmasq_lease = f"/var/lib/troshka/dnsmasq-{vni}.leases"
                dhcp_cmds.append(f"cat > {dnsmasq_conf} << 'DNSEOF'")
                dhcp_cmds.append(f"interface={bridge}")
                dhcp_cmds.append("bind-dynamic")
                dhcp_cmds.append("except-interface=lo")
                dhcp_cmds.append("no-resolv")
                dhcp_cmds.append("no-hosts")
                dhcp_cmds.append(f"pid-file={dnsmasq_pid}")
                dhcp_cmds.append(f"dhcp-leasefile={dnsmasq_lease}")
                dhcp_cmds.append(f"dhcp-range={range_start},{range_end},{lease}")
                for dh in net.get("dhcp_hosts", []):
                    safe_name = (dh.get("name") or "").replace(" ", "-").replace("_", "-")
                    hostname_part = f",{safe_name}" if safe_name else ""
                    dhcp_cmds.append(f"dhcp-host={dh['mac']},{dh['ip']}{hostname_part}")
                if net.get("dns_enabled") and net.get("dns_domain"):
                    dhcp_cmds.append(f"domain={net['dns_domain']}")
                dhcp_cmds.append("DNSEOF")
                dhcp_cmds.append(f"ip netns exec {ns} dnsmasq --conf-file={dnsmasq_conf}")

    # nftables inside namespace
    nft_cmds.append(f"ip netns exec {ns} nft add table inet filter 2>/dev/null || true")
    nft_cmds.append(f"ip netns exec {ns} nft add chain inet filter forward '{{ type filter hook forward priority 0; policy accept; }}' 2>/dev/null || true")
    nft_cmds.append(f"ip netns exec {ns} nft add table inet nat 2>/dev/null || true")
    nft_cmds.append(f"ip netns exec {ns} nft add chain inet nat postrouting '{{ type nat hook postrouting priority 100; }}' 2>/dev/null || true")
    nft_cmds.append(f"ip netns exec {ns} nft add chain inet nat prerouting '{{ type nat hook prerouting priority -100; }}' 2>/dev/null || true")

    # Masquerade outbound traffic from namespace
    nft_cmds.append(f"ip netns exec {ns} nft add rule inet nat postrouting oifname \"{veth_ns}\" masquerade")

    # Intra-bridge forwarding
    for net in config.get("networks", []):
        bridge = net["bridge_name"]
        nft_cmds.append(f"ip netns exec {ns} nft add rule inet filter forward iifname \"{bridge}\" oifname \"{bridge}\" accept")

    # Router: enable forwarding between connected bridges (inside namespace)
    for router in config.get("routers", []):
        for i, vni_a in enumerate(router["connected_vnis"]):
            for vni_b in router["connected_vnis"][i + 1:]:
                br_a = f"br-{vni_a}"
                br_b = f"br-{vni_b}"
                nft_cmds.append(f"ip netns exec {ns} nft add rule inet filter forward iifname \"{br_a}\" oifname \"{br_b}\" accept")
                nft_cmds.append(f"ip netns exec {ns} nft add rule inet filter forward iifname \"{br_b}\" oifname \"{br_a}\" accept")

    # Gateway port forwards (inside namespace) — DNAT from transit IP to VM
    gw = config.get("gateway")
    if gw and gw.get("mode") == "nat-portforward":
        for pf in gw.get("port_forwards", []):
            ext_port = pf.get("extPort", "")
            int_ip = pf.get("intIp", "")
            int_port = pf.get("intPort", "")
            priv_ip = pf.get("_private_ip", "")
            if ext_port and int_ip and int_port:
                # Inside namespace: DNAT arriving traffic to VM
                nft_cmds.append(f"ip netns exec {ns} nft add rule inet nat prerouting tcp dport {ext_port} dnat ip to {int_ip}:{int_port}")
                # Host namespace: DNAT EIP traffic to transit IP
                if priv_ip:
                    host_nft_cmds.append(f"nft add rule inet nat prerouting ip daddr {priv_ip} tcp dport {ext_port} dnat ip to {transit['ns_ip']}:{ext_port}")

    return AGENT_SETUP_SCRIPT.format(
        namespace_commands="\n".join(ns_cmds) or "# No namespace",
        vxlan_commands="\n".join(vxlan_cmds) or "# No VXLAN networks",
        dhcp_commands="\n".join(dhcp_cmds) or "# No DHCP configured",
        nft_commands="\n".join(nft_cmds) or "# No namespace nftables",
        host_nft_commands="\n".join(host_nft_cmds) or "# No host nftables",
    )
```

- [ ] **Step 2: Run tests**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`

- [ ] **Step 3: Commit**

```bash
git add src/backend/app/services/vxlan.py
git commit -m "feat: rewrite generate_setup_script for network namespace isolation"
```

---

### Task 4: Simplify Teardown Scripts

**Files:**
- Modify: `src/backend/app/services/deploy_service.py`

The teardown scripts become much simpler — deleting a namespace cleans up everything inside it.

- [ ] **Step 1: Rewrite generate_network_teardown_script**

Replace the `generate_network_teardown_script` function:

```python
def generate_network_teardown_script(vni_map: dict, project_id: str = "") -> str:
    """Generate a script to tear down project networking by deleting the namespace."""
    pid = project_id[:8] if project_id else ""
    lines = ["#!/bin/bash", "set -uo pipefail", ""]

    if pid:
        ns = f"troshka-{pid}"
        veth_h = f"veth-{pid}-h"
        lines.append(f"ip netns del {ns} 2>/dev/null || true")
        lines.append(f"ip link del {veth_h} 2>/dev/null || true")

    # Clean up dnsmasq config files and leases
    for vni in vni_map.values():
        lines.append(f"rm -f /run/troshka-dnsmasq-{vni}.pid /etc/dnsmasq.d/troshka-{vni}.conf /var/lib/troshka/dnsmasq-{vni}.leases")

    return "\n".join(lines)
```

- [ ] **Step 2: Update generate_destroy_script**

In the `generate_destroy_script` function, replace the network teardown section (after VM undefine, before the final echo) with:

```python
    # Delete namespace (cleans up bridges, VXLAN, dnsmasq, nftables)
    pid = project_id[:8]
    ns = f"troshka-{pid}"
    veth_h = f"veth-{pid}-h"
    lines.append(f"ip netns del {ns} 2>/dev/null || true")
    lines.append(f"ip link del {veth_h} 2>/dev/null || true")
    for vni in vni_map.values():
        lines.append(f"rm -f /run/troshka-dnsmasq-{vni}.pid /etc/dnsmasq.d/troshka-{vni}.conf /var/lib/troshka/dnsmasq-{vni}.leases")
```

Remove the old per-VNI bridge/VXLAN/dnsmasq/nftables cleanup, policy routing cleanup, and per-project chain cleanup.

- [ ] **Step 3: Update incremental script removed networks section**

In `generate_incremental_script`, replace the removed networks section with namespace-aware cleanup. For removed networks, delete the VXLAN and bridge inside the namespace rather than in the host:

```python
    for node in diff["removed_networks"]:
        nid = node["id"]
        if nid in vni_map:
            vni = vni_map[nid]
            ns = f"troshka-{project_id[:8]}"
            lines.append(f"ip netns exec {ns} ip link del br-{vni} 2>/dev/null || true")
            lines.append(f"ip netns exec {ns} ip link del vxlan-{vni} 2>/dev/null || true")
            lines.append(f"rm -f /run/troshka-dnsmasq-{vni}.pid /etc/dnsmasq.d/troshka-{vni}.conf /var/lib/troshka/dnsmasq-{vni}.leases")
```

- [ ] **Step 4: Run tests**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`

- [ ] **Step 5: Commit**

```bash
git add src/backend/app/services/deploy_service.py
git commit -m "feat: simplify teardown — namespace deletion replaces per-resource cleanup"
```

---

### Task 5: Update GC for Namespace Orphans

**Files:**
- Modify: `src/backend/app/services/gc_service.py`

- [ ] **Step 1: Add namespace orphan detection to discover_orphans**

In the `discover_orphans` function, add to the SSH discovery script:

```bash
echo "=== NAMESPACES ==="
ip netns list 2>/dev/null | grep '^troshka-' | awk '{print $1}' || true
```

Then in the parsing section, add:

```python
    namespaces = sections.get("NAMESPACES", [])
    orphaned_namespaces = []
    for ns_name in namespaces:
        ns_pid = ns_name.replace("troshka-", "")
        matched = any(pid.startswith(ns_pid) for pid in active_project_ids)
        if not matched:
            orphaned_namespaces.append(ns_name)
```

Add `orphaned_namespaces` to the return dict.

- [ ] **Step 2: Add namespace cleanup to clean_orphans**

In `clean_orphans`, add after the orphaned bridges section:

```python
    for ns_name in orphans.get("orphaned_namespaces", []):
        pid = ns_name.replace("troshka-", "")
        lines.append(f'echo "Removing orphaned namespace: {ns_name}"')
        lines.append(f"ip netns del {ns_name} 2>/dev/null || true")
        lines.append(f"ip link del veth-{pid}-h 2>/dev/null || true")
```

- [ ] **Step 3: Run tests**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`

- [ ] **Step 4: Commit**

```bash
git add src/backend/app/services/gc_service.py
git commit -m "feat: add namespace orphan detection and cleanup to host GC"
```

---

### Task 6: Update EIP Host DNAT for Transit IPs

**Files:**
- Modify: `src/backend/app/services/eip_service.py`
- Modify: `src/backend/app/api/eips.py`

The host-side EIP DNAT now targets the transit IP instead of the VM IP directly. The `sync_security_group_rules` stays the same (AWS SG doesn't care about namespaces).

- [ ] **Step 1: Update build_host_network_config to include transit info**

In `src/backend/app/services/vxlan.py`, in `build_host_network_config`, add transit subnet info to the gateway config:

```python
    # In the gateway config block, add:
    first_vni = next((net["vni"] for net in networks), None)
    if first_vni:
        transit = _transit_subnet(first_vni)
        gateway_config["transit_ns_ip"] = transit["ns_ip"]
```

- [ ] **Step 2: Update EIP sync endpoint to regenerate host nftables**

In `src/backend/app/api/eips.py`, the `sync_project_eips` endpoint calls `generate_setup_script` which now handles host nftables as part of the namespace setup. No code change needed — the script already includes host nftables for EIP DNAT.

Verify by reading the current code to confirm `generate_setup_script` is called in the EIP sync path.

- [ ] **Step 3: Run tests**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`

- [ ] **Step 4: Commit**

```bash
git add src/backend/app/services/vxlan.py src/backend/app/services/eip_service.py
git commit -m "feat: EIP DNAT targets transit IP for namespace-based networking"
```

---

### Task 7: Update VM Start (Per-VM and Project-Level)

**Files:**
- Modify: `src/backend/app/api/projects.py`
- Modify: `src/backend/app/services/deploy_service.py`

The per-VM start on a stopped project needs to create the namespace (not just bridges). The project-level start already calls `generate_setup_script` which now creates the namespace.

- [ ] **Step 1: Simplify per-VM start on stopped project**

In `src/backend/app/api/projects.py`, the per-VM start on a stopped project currently has inline namespace/bridge/EIP setup. Replace with a call to `start_project_async` style setup — the `generate_setup_script` now handles everything:

The background thread in `_start_infra_then_vm` should call `generate_setup_script` with the project_id. The namespace creation is idempotent (deletes and recreates). The qemu hook handles tap migration automatically.

No code change needed if the current `_start_infra_then_vm` already calls `generate_setup_script` — verify this is the case.

- [ ] **Step 2: Remove old policy routing and per-project chain cleanup from per-VM start**

Remove any remaining references to:
- `ip rule` / `ip route table` commands
- `troshka-fwd/post/pre-*` chain names
- `bind-dynamic` dnsmasq references

These are replaced by namespace operations in `generate_setup_script`.

- [ ] **Step 3: Run tests**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`

- [ ] **Step 4: Commit**

```bash
git add src/backend/app/api/projects.py src/backend/app/services/deploy_service.py
git commit -m "feat: per-VM and project start use namespace-based networking"
```

---

### Task 8: Integration Test

- [ ] **Step 1: Run full backend test suite**

Run: `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
Expected: all tests pass.

- [ ] **Step 2: Restart backend and test**

Restart backend. Stop both projects. Start both projects. Verify:
- Both projects' VMs get DHCP IPs
- Both projects' VMs can ping 8.8.8.8
- All 4 EIPs return 200
- `ip netns list` on host shows two `troshka-*` namespaces
- Each namespace has its own bridge, dnsmasq, nftables

- [ ] **Step 3: Test stop/start cycle**

Stop project1, verify project2 still works. Start project1, verify both work.

- [ ] **Step 4: Commit any fixups**

```bash
git add -A && git commit -m "fix: namespace integration fixups"
```
