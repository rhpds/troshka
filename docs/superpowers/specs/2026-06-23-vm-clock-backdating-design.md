# VM Clock Backdating

**Date**: 2026-06-23
**Status**: Draft

## Overview

Allow Troshka projects to run VMs with clocks set to a specific past (or future) date. The primary use case is simulating a target date so software inside VMs believes it's that date — useful for testing certificate expiry, license validation, time-gated features, and reproducing time-sensitive bugs.

## Requirements

- **Project-level setting**: a single `clock_target` datetime applies to all VMs in the project
- **Set at deploy or live**: can be configured before deploy (applied at VM creation) or changed on a running project (pushed immediately)
- **Real-time advancement**: clock starts at the target date and ticks forward at normal speed
- **NTP service on gateway**: every project's gateway runs chrony as a local NTP server — always, not just when backdating. When `clock_target` is set, the gateway serves the backdated time. When unset, it serves real time.
- **All VMs point at gateway NTP**: VMs never use public NTP pools. Chrony on each VM is configured to sync from the gateway only.
- **Template support**: `clock_target` is a top-level field in `infra_template.yaml`
- **Hypervisor-level offset**: the clock offset is applied at the libvirt/QEMU level so the guest sees the target time from the moment the BIOS/UEFI clock is read, before the OS boots

## Design

### Data Model

Add one nullable column to the Project model:

```python
clock_target: Mapped[Optional[datetime]] = mapped_column(
    DateTime(timezone=True), nullable=True
)
```

- `NULL` = real time (no clock manipulation)
- Any value = all VMs see that datetime as "now" at deploy time, ticking forward in real time

Alembic migration adds the column with `nullable=True`, no default.

### Template YAML

Top-level field, optional:

```yaml
clock_target: "2025-01-15T00:00:00Z"

networks:
  ocp:
    cidr: 192.168.47.0/24
    ...
vms:
  bastion:
    ...
```

- `resolve_inline_template()` passes `clock_target` through to the resolved dict
- The import endpoint (`POST /projects/{id}/import-template`) sets it on the Project model directly (not in topology JSONB)
- `export_topology_to_template()` includes `clock_target` in the output YAML if set on the project
- `POST /projects/from-template` also reads it

### API Changes

**ProjectUpdate schema** — add optional field:

```python
clock_target: Optional[datetime] = None
```

**PATCH /projects/{id}** — when `clock_target` is updated on an `active` project, trigger the live adjustment flow (below) as a background operation.

### Deploy Pipeline — VM Clock Offset

At deploy time, when `project.clock_target` is set:

**Offset calculation** (in `deploy_service.py`):

```python
offset_seconds = int((clock_target - datetime.now(timezone.utc)).total_seconds())
```

This produces a negative number for past dates (e.g., ~-47,304,000 for 1.5 years ago).

**VM params** — add `clock_offset` to the dict passed to troshkad:

```python
if clock_target:
    params["clock_offset"] = offset_seconds
```

**troshkad virt-install** — when `clock_offset` is present, add to the command:

```
--clock offset=variable,adjustment={clock_offset}
```

`offset=variable` starts the VM at `UTC + adjustment` and ticks forward in real time. The adjustment is persisted in the domain XML and libvirt recalculates the delta on VM restart.

Applied to every VM in the project uniformly — same offset for the gateway and all other VMs.

### Gateway NTP Server

The gateway always runs chrony as a local NTP server, regardless of whether `clock_target` is set. This isolates VMs from external NTP and provides a consistent time source within the project.

**Gateway cloud-init** (always, on every project):

- Package: `chrony`
- Write `/etc/chrony.conf`:
  ```
  local stratum 3
  allow 192.168.0.0/16
  driftfile /var/lib/chrony/drift
  ```
  No `server` or `pool` lines — the gateway trusts its own hardware clock.
- runcmd: `systemctl restart chronyd`

**All other VMs** cloud-init (always, on every project):

- Package: `chrony`
- Write `/etc/chrony.conf`:
  ```
  server <gateway_ip> iburst prefer
  makestep 1 -1
  driftfile /var/lib/chrony/drift
  ```
  `makestep 1 -1` = step the clock immediately on any offset, unlimited times.
  No public NTP pools.
- runcmd: `systemctl restart chronyd`

When `clock_target` is unset, the gateway's clock is real time, so it serves real time. When set, the hypervisor offset makes the gateway's clock read the target date, and chrony serves that to all VMs.

### Live Adjustment

When a user changes `clock_target` on a running (`active`) project:

**Step 1 — Update the Project model** with the new `clock_target` value (or `NULL` to clear). Recompute `offset_seconds`.

**Step 2 — Update each VM's libvirt XML** for reboot persistence:

- New troshkad endpoint: `POST /vms/set-clock`
- Params: `{"domain_name": "...", "offset_seconds": N}` (or `"offset_seconds": null` to reset to `offset=utc`)
- Uses existing `virsh dumpxml` → XML edit → `virsh define` pattern to update the `<clock>` element

**Step 3 — Push the time to running VMs immediately:**

- For each running VM, try `virsh domtime --set <target_epoch>` (uses qemu-guest-agent)
- If that fails (agent not installed/running), fall back to exec: `date -s @<target_epoch>` via serial exec
- Gateway is updated first, then other VMs — by the time VMs re-sync via NTP, the gateway is already serving the new time

**Step 4 — NTP re-sync:**

- No chrony config changes needed — the gateway always serves `local stratum 3` from its own clock
- Other VMs re-sync automatically within seconds

**Clearing `clock_target`** (setting to `null`):

- Update libvirt XML back to `offset=utc` on all VMs
- Push real time to VMs via the same guest-agent/exec fallback
- NTP re-syncs to real time automatically (gateway's clock is now real)

### Frontend — Project Settings

Add a "Clock" section to the Project Settings page (`/app/projects/[id]/page.tsx`), positioned below the auto-stop/auto-delete timers:

- **Date/time picker**: PatternFly `DatePicker` + `TimePicker` for `clock_target`
- **Label**: "Target Date" with helper text: "Set all VM clocks to this date. Leave empty for real time."
- **Clear button**: resets to `null` (real time)
- **Offset indicator**: when set, display "VMs are running X months, Y days behind real time"
- **Save**: uses `PATCH /projects/{id}` with the `clock_target` field
- **Toast**: on save for active projects, show "Clock updated — VMs syncing"

**State-dependent behavior:**

| Project State | Save Behavior |
|---|---|
| `draft` | Saves value, applied at deploy time |
| `active` | Saves and triggers live adjustment (background) |
| `deploying`, `stopping`, `error` | Saves value, no live push — offset applies on next VM start |

## Not In Scope

- Per-VM clock offsets (all VMs share the project-level setting)
- Frozen clocks (clock always ticks forward at real speed)
- Timezone configuration (VMs use UTC; timezone is a guest OS concern)
- Custom NTP server addresses (gateway is always the sole NTP source)
