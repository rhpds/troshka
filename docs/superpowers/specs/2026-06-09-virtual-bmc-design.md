# Virtual BMC (IPMI & Redfish) for Troshka VMs

**Date:** 2026-06-09
**Status:** Draft
**Goal:** Enable OpenShift bare-metal installation (IPI, UPI, Agent-Based) by providing virtual BMC endpoints for VMs.

---

## Motivation

OpenShift bare-metal installs require BMC (Baseboard Management Controller) access to manage host power state, boot devices, and virtual media. IPI uses Ironic, which speaks Redfish (preferred) or IPMI to control nodes. Troshka VMs are libvirt domains — we can emulate BMC interfaces using `sushy-tools` (Redfish) and `virtualbmc` (IPMI), translating protocol commands into libvirt API calls.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Protocols | Redfish + IPMI | Redfish for OpenShift IPI/ABI, IPMI for `ipmitool` ad-hoc use |
| BMC software | `sushy-tools` + `virtualbmc` in a venv | Battle-tested against Ironic; keeps troshkad stdlib-pure |
| Network model | Dedicated BMC network on canvas | Realistic management LAN; user explicitly wires provisioner |
| BMC per VM | One sushy-emulator + one vbmc entry per VM | Each VM gets its own BMC IP — matches real iDRAC/iLO/BMC |
| System ID | Libvirt domain name | sushy-emulator uses domain UUID/name; predictable and unique |
| Credentials | Per-project, stored in topology | Preserved in patterns so lab instructions stay stable |
| BMC opt-in | Per-VM toggle | Not every VM needs a BMC (helper/bastion VMs typically don't) |
| BMC network lifecycle | Auto-created/removed | Appears when first VM enables BMC, disappears when last disables |
| Deploy validation | Blocks if no provisioner connected | BMC endpoints are useless without a VM that can reach them |

## Architecture

### BMC Network

A dedicated Linux bridge `br-bmc-{project_id[:8]}` created inside the project's network namespace. Only exists if at least one VM has BMC enabled.

- **CIDR:** `192.168.100.0/24` (default, editable on the BMC network node)
- **Gateway:** `.1` (namespace-side veth)
- **VM BMC IPs:** `.11`, `.12`, `.13`... assigned in topology order to BMC-enabled VMs
- **Provisioner connectivity:** The provisioner VM is manually connected to the BMC network via a canvas edge — it gets a real NIC on this bridge
- **BMC-enabled VMs do NOT get a NIC** on this network. Their BMC endpoints (sushy-emulator, vbmc) listen on the BMC IP, but the VM itself has no interface on the BMC bridge.

### BMC Endpoints Per VM

For each BMC-enabled VM:

- **Redfish:** One `sushy-emulator` process bound to `{bmc_ip}:8000`, configured with `SUSHY_EMULATOR_ALLOWED_INSTANCES` restricted to that single libvirt domain. Feature set: `vmedia` (required for OpenShift IPI `redfish-virtualmedia://` scheme).
- **IPMI:** One `vbmc` entry registered with the project's `vbmcd`, bound to `{bmc_ip}:623`.

For a project with 3 BMC-enabled VMs: 3 sushy-emulator processes + 1 vbmcd (managing 3 entries).

### Resulting BMC Addresses

```
Redfish:  redfish-virtualmedia://192.168.100.11:8000/redfish/v1/Systems/troshka-a1b2c3d4-e5f6g7h8
IPMI:     192.168.100.11:623  (user: admin, pass: <random>)
```

### Credentials

- Username: `admin` (default, editable)
- Password: randomly generated when the BMC network first auto-materializes
- Stored on the BMC network node in topology JSONB — preserved in patterns
- Shared across all BMC endpoints in the project
- Editable in the BMC network properties panel

## Host Setup & Dependencies

### Agent Installer Changes

During agent install (`agent_deployer.py`), create a Python venv with BMC tools:

```bash
python3 -m venv /opt/troshka/venv
/opt/troshka/venv/bin/pip install sushy-tools virtualbmc libvirt-python
```

Additional system packages added to the DNF install list: `python3-devel`, `pkg-config` (needed for `libvirt-python` compilation).

### Troshkad Endpoints

| Endpoint | Purpose |
|----------|---------|
| `/commands/bmc/setup` | Create BMC bridge, start vbmcd + sushy-emulators for all BMC-enabled VMs |
| `/commands/bmc/teardown` | Stop all BMC processes, remove BMC bridge |
| `/commands/bmc/status` | Return running state of all BMC processes |

### Host File Layout

Per-project config at `/var/lib/troshka/bmc/{project_id}/`:

- `sushy-{vm_id[:8]}.conf` — per-VM sushy-emulator config
- `vbmcd.conf` — vbmcd daemon config
- `htpasswd` — shared credentials file for sushy basic auth

## Topology & Data Model

### VM Node Data — New Fields

```json
{
  "data": {
    "bmcEnabled": false,
    "bmcIp": "192.168.100.11"
  }
}
```

- `bmcEnabled`: toggle in the properties panel
- `bmcIp`: auto-assigned when BMC is enabled, cleared when disabled

### BMC Network Node — Auto-Generated

```json
{
  "id": "bmc-network",
  "type": "networkNode",
  "data": {
    "name": "BMC Network",
    "networkType": "bmc",
    "cidr": "192.168.100.0/24",
    "dhcpEnabled": true,
    "bmcUsername": "admin",
    "bmcPassword": "xK9m2..."
  }
}
```

- Auto-created when first VM enables `bmcEnabled`
- Auto-removed when last VM disables `bmcEnabled` (edges cleaned up)
- Only one per project
- CIDR must not overlap with other networks in the topology

### Deploy State — BMC Addresses

After deploy, BMC addresses stored in `Project.deploy_state` JSONB for UI display:

```json
{
  "bmc": {
    "password": "xK9m2...",
    "username": "admin",
    "vms": {
      "vm-node-id": {
        "ip": "192.168.100.11",
        "redfish_url": "redfish-virtualmedia://192.168.100.11:8000/redfish/v1/Systems/troshka-a1b2c3d4-e5f6g7h8",
        "ipmi_address": "192.168.100.11:623"
      }
    }
  }
}
```

## Frontend UX

### VM Properties Panel — BMC Section

- **Toggle:** "Enable BMC" checkbox
  - On: assigns next available BMC IP, auto-creates BMC network node if needed
  - Off: releases BMC IP, auto-removes BMC network if no other VMs have BMC enabled
- **When deployed (read-only):**
  - Redfish URL — with copy-to-clipboard button
  - IPMI Address — with copy-to-clipboard button
  - Username — with copy-to-clipboard button
  - Password — masked by default, click to reveal, with copy-to-clipboard button

### BMC Network Node on Canvas

- Visually distinct from regular networks (different icon or color accent)
- Properties panel: name, CIDR (editable), username (editable), password (editable, masked with reveal toggle)
- **Warning banner:** "Connect a provisioner VM to this network to enable BMC access" — shown when no VMs have edges to the BMC network
- Lists BMC-enabled VMs and their assigned IPs (informational, not editable)

### Deploy Validation

- BMC network exists with no connected VMs → **block deploy** with error: "BMC network requires at least one connected VM to act as a provisioner"

## Deploy Flow Integration

### Deploy Sequence (Updated)

1. Network setup (VXLAN, bridges, DHCP)
2. **BMC bridge setup** — create `br-bmc-{project_id[:8]}` inside namespace, assign gateway IP
3. Cloud-init seed ISOs
4. Image caching
5. PXE setup
6. Disk creation
7. VM definition
8. **BMC endpoint startup** — start vbmcd, register vbmc entries, start sushy-emulator processes (requires libvirt domains to exist)
9. VM startup (respecting start order)

BMC endpoints start after VM definition but before VM startup so Ironic can reach them immediately when the provisioner boots.

### Undeploy Sequence (Updated)

1. VM shutdown/destroy
2. **BMC teardown** — stop sushy-emulators, stop vbmcd, remove BMC bridge
3. Network teardown

### Topology Remapping

When cloning topology (patterns, project duplication):
- BMC network node ID is remapped
- VM `bmcIp` values are reassigned from the BMC CIDR
- `bmcPassword` is **preserved** (pattern lab instructions stay stable)
- All other standard remapping applies (node IDs, edge references, NICs, etc.)

## Ironic Compatibility

### Redfish Endpoints Used by Ironic

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/redfish/v1/Systems` | GET | Enumerate systems |
| `/redfish/v1/Systems/{id}` | GET/PATCH | System info, boot source override |
| `/redfish/v1/Systems/{id}/Actions/ComputerSystem.Reset` | POST | Power on/off/reset |
| `/redfish/v1/Managers/{id}/VirtualMedia` | GET | List virtual media devices |
| `/redfish/v1/Managers/{id}/VirtualMedia/Cd` | GET | Check current media state |
| `/redfish/v1/Managers/{id}/VirtualMedia/Cd/Actions/VirtualMedia.InsertMedia` | POST | Mount ISO |
| `/redfish/v1/Managers/{id}/VirtualMedia/Cd/Actions/VirtualMedia.EjectMedia` | POST | Unmount ISO |

`sushy-tools` with feature set `vmedia` handles all of these out of the box.

### IPMI Operations Supported by virtualbmc

| Operation | ipmitool command |
|-----------|-----------------|
| Power on | `power on` |
| Power off | `power off` |
| Soft shutdown | `power soft` |
| Power reset | `power reset` |
| Power status | `power status` |
| Set boot device | `chassis bootdev pxe\|disk\|cdrom` |

### install-config.yaml Example

```yaml
platform:
  baremetal:
    hosts:
      - name: master-0
        role: master
        bmc:
          address: redfish-virtualmedia://192.168.100.11:8000/redfish/v1/Systems/troshka-a1b2c3d4-e5f6g7h8
          username: admin
          password: xK9m2...
        bootMACAddress: 52:54:00:aa:bb:01
      - name: master-1
        role: master
        bmc:
          address: redfish-virtualmedia://192.168.100.12:8000/redfish/v1/Systems/troshka-a1b2c3d4-11223344
          username: admin
          password: xK9m2...
        bootMACAddress: 52:54:00:aa:bb:02
```

## Garbage Collector Integration

The existing GC runs on host agent connect, admin Clean button, or future cron. BMC adds a new cleanup step:

### Orphan BMC Cleanup

During the orphan cleanup phase, the GC checks for:

- **Orphaned sushy-emulator processes** — sushy-emulator processes whose libvirt domain no longer exists (VM was destroyed but BMC wasn't torn down cleanly)
- **Orphaned vbmc entries** — vbmc registrations pointing at non-existent domains
- **Orphaned vbmcd processes** — vbmcd daemons for projects that no longer have any deployed VMs
- **Stale BMC config dirs** — `/var/lib/troshka/bmc/{project_id}/` directories for projects that are no longer deployed
- **Orphaned BMC bridges** — `br-bmc-*` bridges inside namespaces with no corresponding deployed project

### Cleanup Actions

1. Kill orphaned sushy-emulator processes (match by PID file or process args)
2. `vbmc stop` + `vbmc delete` for orphaned entries
3. Kill orphaned vbmcd processes
4. Remove stale `/var/lib/troshka/bmc/{project_id}/` directories
5. Delete orphaned `br-bmc-*` bridges

This runs as part of the existing GC sequence, after VM orphan cleanup and before cache eviction.

## Out of Scope (Future)

- Serial-over-LAN (SOL) emulation
- BMC sensor/thermal emulation
- BMC user management (multiple users per BMC)
- Firmware update emulation
- IPMI-only mode (without Redfish)
