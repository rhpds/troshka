# Network router day-0 bootstrap

How virtual routers get their initial configuration in the **net-automation
workshop**, what **vrnetlab** actually does inside Containerlab, and how
**Troshka** should match it.

Sources reviewed (Aug 2026):

- [rhpds/zt-network-automation-workshop](https://github.com/rhpds/zt-network-automation-workshop) — lab inventory, deploy playbooks, `setup-containerlab.sh`
- [srl-labs/vrnetlab](https://github.com/srl-labs/vrnetlab) — `juniper/vsrx/docker/launch.py`, `cisco/c8000v/docker/launch.py`, `arista/veos/docker/launch.py`
- Troshka: `troshkad` serial handlers, `kubevirt_serial.py`, `demo_workloads` `configure_routers` role

## Container lab vs Troshka workshop

| | Container lab (original RHDP) | Troshka `net-automation-workshop` |
|---|---|---|
| Runtime | One **containerlab** VM runs four vrnetlab containers; each container launches a **QEMU VM** inside | Four separate **vmNodes** (libvirt or KubeVirt) |
| Topology file | `routers.clab.yml` baked into `ansiblebu-containerlab-v3` image at `/home/lab-user/1_multi_vendor_router/` | `example_templates/net-automation-workshop.yaml` |
| Day-0 config | vrnetlab `launch.py` at container start | Ansible `configure_routers` via Troshka API `method: serial` (`demo_workloads`) |
| Student Ansible | NETCONF/SSH to mgmt IPs (or LB ports 2222–2226) | Same after bootstrap |

**Important:** vrnetlab is not “containers instead of VMs.” Containerlab starts a
container; vrnetlab starts QEMU inside it. The bootstrap logic lives in each
kind’s `launch.py`, not in Containerlab itself.

## Per-router bootstrap in vrnetlab

The workshop topology (`1_multi_vendor_router`) has four routers. Each vrnetlab
kind uses a **different** day-0 mechanism. Only Arista vEOS uses serial to
**apply** configuration.

| Node | Kind / image | Config mechanism | Serial console role |
|------|----------------|------------------|---------------------|
| **rtr1** | `cisco_c8000v` / `cat8:17.13.01a` | **ISO CD-ROM** | Watch boot logs for `CVAC-4-CONFIG_DONE`; config loaded from ISO, not typed on UART |
| **rtr2, rtr4** | `arista_veos` / `veos-ee:4.32.0F` | **Serial interactive** | Wait for `login:`, log in, type `enable` / `configure` / lines from `startup-config.cfg` |
| **rtr3** | `juniper_vsrx` / `juniper-ee:23.2R2.21` | **ISO CD-ROM** | Health check only — wait for `login:`, then close serial; config **not** replayed on UART |

### rtr1 — Cisco C8000v (ISO, not serial config)

`cisco/c8000v/docker/launch.py`:

1. Builds IOS-XE config text: `gen_bootstrap_config()` (hostname, vrf, Gi1 mgmt,
   SSH, NETCONF, RESTCONF, user) plus optional `startup-config.cfg` append.
2. Writes `iosxe_config.txt` (or MIME-wrapped `ciscosdwan_cloud_init.cfg` in
   controller mode).
3. Runs `genisoimage` → `config.iso`.
4. QEMU: `-cdrom /config.iso`.
5. IOS-XE **CVAC** reads the ISO at boot and applies config.
6. Serial is connected to watch for `CVAC-4-CONFIG_DONE` / factory-reset
   messages — not to paste config.

First-time **install** images use a separate install ISO that includes
`platform console serial` so later boots use UART; that is still ISO-driven, not
an interactive serial paste of the full startup config.

Workshop note: the baked `startup-config.cfg` on some images had a premature
`end` before `line vty` / NETCONF blocks, which broke CVAC replay and added
minutes to boot — see
`README-c8000v-performance.md` in zt-network-automation-workshop.

### rtr2 / rtr4 — Arista vEOS (serial config)

`arista/veos/docker/launch.py`:

1. `bootstrap_spin()` waits for `login:` on the QEMU serial port.
2. Logs in as `admin` (empty password at first boot).
3. `bootstrap_config()` — interactive serial session:
   `enable` → `configure` → mgmt IP, default route, eAPI, gNMI, NETCONF,
   hostname, `copy running-config startup-config`.
4. `startup_config()` — if `/config/startup-config.cfg` exists, enters
   `configure terminal` and **types each line** from the file over serial, then
   `end` and save.

This is the path Troshka’s `serial-eos` handler is meant to mirror.

### rtr3 — Juniper vSRX (ISO, not serial config)

`juniper/vsrx/docker/launch.py`:

1. Base template `init.conf` (hierarchical `juniper.conf` format): hostname,
   root/admin passwords, SSH, NETCONF, `fxp0` mgmt, static route in
   `mgmt_junos`.
2. If `startup-config.cfg` exists, append it: `cat init.conf startup-config.cfg >> juniper.conf`.
3. `make-config-iso.sh juniper.conf config.iso` — ISO with `config/juniper.conf`.
4. QEMU: `-drive if=ide,...,file=/config.iso,media=cdrom`.
5. Junos **KVM bootstrap** applies `juniper.conf` from the mounted ISO at boot
   ([Juniper KVM bootstrap docs](https://www.juniper.net/documentation/us/en/software/vsrx/vsrx-consolidated-deployment-guide/vsrx-kvm/topics/task/security-vsrx-kvm-bootstrap-config.html)).
6. Serial waits for `login:` only to mark the container healthy, then disconnects.

Containerlab’s `startup-config:` knob copies the user file into the container
as `/config/startup-config.cfg`; vrnetlab merges it into the ISO. It does **not**
reliably “replay over serial” for vSRX (community reports match our Troshka
experience).

## What Troshka has today

| Capability | libvirt (troshkad) | KubeVirt |
|------------|-------------------|----------|
| Hypervisor serial port | `isa-serial` PTY | `autoattachSerialConsole` + WebSocket |
| `serial_exec: ios` | Works (rtr1 tested) | Implemented (`kubevirt_serial.py`) |
| `serial_exec: eos` | Works | Implemented |
| `serial_exec: junos` | Code present; **vSRX guest silent on UART** | Same |
| Workshop day-0 | `configure_routers` → API `method: serial` + `rtr3-junos.j2` (`set` commands) | Same |
| CD-ROM attach | troshkad reconfigure `cdroms` | `spec.cdrom` on TroshkaVM |
| ISO generation | cloud-init seed ISOs only | same |

The Junos serial handler (`_handle_vm_serial_exec_junos`) expects FreeBSD
`login:` → `cli` → `configure` / `set` / `commit`. That matches **vEOS-style**
serial bootstrap, not what vrnetlab does for vSRX. On stock vSRX images the
guest console is often on **VGA (`ttyv0`)**, so the hypervisor serial is empty
even though IOS routers work.

`demo_workloads` `rtr3-junos.j2` is equivalent in *intent* to vrnetlab’s merged
`juniper.conf`, but in `set` syntax for serial CLI — not the hierarchical format
the ISO bootstrap expects.

## Target: match vrnetlab per platform (Option 1 focus)

Planned direction: implement bootstrap the way each NOS expects, not one serial
path for everything.

```
┌──────────┬─────────────────────┬──────────────────────────────────────────┐
│ Router   │ vrnetlab            │ Troshka (planned)                        │
├──────────┼─────────────────────┼──────────────────────────────────────────┤
│ rtr1     │ config.iso (IOS-XE) │ IOS-XE bootstrap ISO at deploy (later)   │
│ rtr2/rtr4│ serial paste        │ serial_exec: eos (existing; works)       │
│ rtr3     │ config.iso (Junos)  │ Juniper bootstrap ISO at deploy (Option 1)│
└──────────┴─────────────────────┴──────────────────────────────────────────┘
```

### Option 1 — Bootstrap ISO in library (rtr1, rtr3)

Implemented for the net-automation workshop:

1. Config sources in `example_templates/net-automation-workshop/bootstrap/`.
2. `./scripts/build-router-bootstrap-isos.sh --upload` → Troshka library items
   `net-automation-rtr1-bootstrap`, `net-automation-rtr3-bootstrap`.
3. Template `isos:` attaches SATA CD-ROM (`bootableIso: false` — boot stays on disk).
4. Cisco CVAC / Junos KVM bootstrap apply config at first boot (vrnetlab-compatible).

Touchpoints:

- `scripts/build-router-bootstrap-isos.sh`
- `example_templates/net-automation-workshop.yaml` — `isos` on rtr1/rtr3
- `template_loader.py` — cdrom controller for `os: blank` + `isos`
- `deploy_topology.py` — non-bootable ISO does not reorder boot to cdrom-first

### Juniper ISO bootstrap (rtr3) — implementation notes

At deploy time (VM **stopped**):

1. Render `juniper.conf` from template (port `init.conf` + workshop overrides:
   hostname, `fxp0`, admin user, SSH, NETCONF).
2. `mkisofs` → `config.iso` (same layout as vrnetlab: `config/juniper.conf` on
   ISO).
3. Attach as SATA CD-ROM (troshkad / KubeVirt `spec.cdrom`).
4. Start VM; Junos applies config on first boot.
5. Optionally detach ISO after successful boot (or leave for redeploy policy).

Implementation touchpoints (not yet built):

- Template field or role template for `juniper.conf` (hierarchical, not `set`).
- troshkad endpoint to build and place bootstrap ISO.
- Deploy / operator: attach cdrom for `serial_exec: junos` (or explicit
  `junosBootstrap: true`) when disk is not from a golden pattern.
- Keep `configure_routers` + serial as fallback / `force_router_bootstrap`.

### Fallbacks

- **guestfish** `/boot/loader.conf` (`comconsole`, `boot_serial`) — force vSRX
  onto UART so existing serial exec + `rtr3-junos.j2` work; more fragile than ISO.
- **Golden pattern** — bootstrap once, capture disk; redeploys skip day-0
  (`PATTERN-BUILD.md`).

## Workshop inventory reference

Container lab (`network-workshop/lab_inventory/hosts`):

```
rtr3 ansible_host=containerlab ansible_port=2225 ansible_netconf_port=2224
```

Troshka workshop (`demo_workloads/.../lab_inventory/hosts`):

```
rtr3 ansible_host=172.20.20.30
```

After day-0, both use `ansible_connection: netconf` and `admin` / `admin@123`.

## Related files

| Path | Purpose |
|------|---------|
| `example_templates/net-automation-workshop.yaml` | Troshka topology; `serial_exec: junos` on rtr3 |
| `~/demo_workloads/.../configure_routers.yml` | Serial bootstrap via Troshka API |
| `~/demo_workloads/.../rtr3-junos.j2` | Day-0 `set` commands (serial path) |
| `src/troshkad/troshkad.py` | `_handle_vm_serial_exec_junos` |
| `src/backend/app/services/providers/kubevirt_serial.py` | KubeVirt serial exec |
| `src/operator/handlers/vm.py` | `guestfishCommands` job |
| `src/operator/helpers/kubevirt.py` | `_add_cdrom_if_present` |
