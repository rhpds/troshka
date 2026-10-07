# VM pause and hibernate (project + per-VM)

**Date:** 2026-10-07  
**Status:** Draft

Add first-class **Pause** and **Hibernate** alongside existing Start/Stop, at both **project** and **per-VM** scope. Project toolbar uses a split Off control whose mode is persisted. Metering: pause keeps compute billing; hibernate stops vCPU/RAM billing. Both pause and hibernate warn that guest services may not resume cleanly.

Related: [`2026-10-05-project-metering-design.md`](./2026-10-05-project-metering-design.md).

## Goals

- Project-level `power_off_mode`: `stop` | `pause` | `hibernate` (default **`stop`**).
- Toolbar **B**: primary Off button label follows mode + ▾ mode picker + Start; mode change alone does not power VMs.
- Per-VM Pause / Hibernate / Start-or-resume; multi-select uses the same actions.
- **Pause** on troshkad/libvirt and KubeVirt (libvirt suspend / VMI pause subresource).
- **Hibernate** on troshkad/libvirt only in v1 (`managedsave`); KubeVirt UI greyed + API unsupported.
- Pause: vCPU/RAM meter **keeps running**. Hibernate: vCPU/RAM meter **stops**; disks / EIPs / Ceph unchanged.
- Confirm dialog for Pause/Hibernate with mileage warning + “Don’t show again for this project.”
- Start cascades: unpause | restore-from-managedsave | cold start by current VM state.

## Non-goals (v1)

- KubeVirt native or Troshka-owned RAM save/restore (upstream hibernate VEP not shipped).
- Guest ACPI S4 as a substitute for hibernate.
- Silently mapping KubeVirt Hibernate → Stop.
- Metering the hibernate save image as its own line item.
- Changing dedicated-host billing semantics beyond guest live-state rules below.

## Concepts

| State | Hypervisor meaning | vCPU/RAM meter |
| --- | --- | --- |
| `running` | Domain/VMI active | On |
| `paused` | Suspended; QEMU/virt-launcher still hold RAM | **On** |
| `hibernated` | managedsave complete; QEMU gone; save image on host | **Off** |
| `stopped` / `shut_off` | Clean stop | Off |

**Capability matrix**

| Action | troshkad / libvirt | KubeVirt |
| --- | --- | --- |
| Pause / unpause | Yes | Yes |
| Hibernate / restore | Yes | No (v1) |
| Stop / start | Yes (existing) | Yes (existing) |

Pause freezes CPUs/I/O but keeps host RAM. Hibernate writes memory state to disk and frees host RAM (needs free disk roughly on the order of guest RAM).

## UX

### Project toolbar

- Replace plain ■ Stop with split control: **[ Off label ▾ ][ ▶ Start ]** when the environment can be powered down / up.
- Off label: Stop / Pause / Hibernate from `power_off_mode`.
- ▾ sets `power_off_mode` via project PATCH; does not execute power.
- On KubeVirt hosts: Hibernate menu entry disabled with tooltip (not supported yet); `power_off_mode` may only be `stop` or `pause` (reject PATCH to `hibernate`).
- Off click: if mode is `pause` or `hibernate` and warn not dismissed → confirm; optional “Don’t show again for this project” → set `power_warn_dismissed`.
- Off executes existing **`POST /projects/{id}/stop`**, extended to honor `power_off_mode`.
- Start executes existing **`POST /projects/{id}/start`**, extended to resume each VM by state.

### Per-VM / selection

- Context menu: Start/Resume; Graceful Shutdown; Pause; Hibernate (greyed on KubeVirt); Force Off.
- Node Off control exposes the same three off actions (split or menu).
- Multi-select toolbar mirrors project Off modes + Start for selected VMs only.

### Confirm copy

Shared for pause and hibernate:

> This freezes the guest without a clean service shutdown. Databases, clusters, and network services may not recover cleanly when resumed. Mileage may vary.

Hibernate adds: *Host RAM is freed; a save image is kept on disk.*

### Status chrome

- Distinct badges: Paused vs Hibernated vs Stopped (do not label pause as Stopped).
- Live spend: paused VMs still count for compute; hibernated do not.

## API and backend

### Project fields

On `Project` (migration):

- `power_off_mode`: string enum `stop` | `pause` | `hibernate`, default `stop`
- `power_warn_dismissed`: boolean, default `false`

Exposed on project GET/PATCH.

### Per-VM endpoints

Alongside existing start/stop/forcestop:

- `POST /projects/{id}/vms/{vm_id}/pause`
- `POST /projects/{id}/vms/{vm_id}/unpause`
- `POST /projects/{id}/vms/{vm_id}/hibernate`

`POST .../start` is the unified bring-back: unpause if paused, start (libvirt restores managedsave automatically) if hibernated, cold start if stopped. Explicit `/unpause` remains for callers that only need resume-from-pause.

### Project endpoints

- Extend `POST /projects/{id}/stop` to apply `power_off_mode` to running VMs (skip already non-running). Optional body `{ "vm_ids": [...] }` for selection scope; omit = all project VMs.
- Extend `POST /projects/{id}/start` to cascade resume/start per VM state (same optional `vm_ids`).
- `PATCH` of `power_off_mode=hibernate` on a KubeVirt project returns `hibernate_unsupported`. Executing Off while mode is hibernate on KubeVirt also returns that error and does **not** stop.

### Host capability

Surface `supports_hibernate` (and reuse pause support as always-true for libvirt + kubevirt guests) on host/project payload so the UI can grey Hibernate without guessing `host_type`.

### troshkad

- `/vms/pause`, `/vms/resume` → libvirt suspend / resume
- `/vms/hibernate` → `managedsave`
- Start path: unchanged libvirt start after managedsave restores saved state
- Domain destroy / project destroy: ensure managedsave images are cleared with the domain (libvirt usual lifecycle)

### KubeVirt

- Pause / unpause via VMI subresources (same family as existing host `unpause_host`)
- Hibernate → structured unsupported response, e.g. HTTP 501 or 409 with `code: hibernate_unsupported` — never map to Halted

### Jobs and websocket

- Hibernate may be slow → background job + `vm-state` notifications (`hibernating` → `hibernated` or failure)
- Pause usually fast; still emit `vm-state`
- Failures mid-hibernate (e.g. disk full): do not mark `hibernated`; leave prior running/paused state if libvirt did; surface host error

### Project state

- User pause: project remains **active** for metering (compute still accruing).
- Project may move toward `stopped` only when no VMs remain `running` or `paused` (all stopped and/or hibernated), consistent with “compute idle” without treating pause as stop.
- Do not conflate guest user-pause with OCP Virt host IO-error pause.

## Metering

Today `paused` is incorrectly in `_STOPPED_STATES` in `metering_math.py`. Change:

- **`paused`**: treat as **running** for vCPU/RAM (remove from stopped set).
- **`hibernated`**: treat as **stopped** for vCPU/RAM (add to stopped set).
- Disks / EIPs / Ceph: unchanged (accrue while they exist).
- Event hooks / reconcile: open/close compute intervals on transitions into/out of hibernated and stopped; pause does not close compute intervals.

## Errors and edge cases

| Case | Behavior |
| --- | --- |
| Hibernate on KubeVirt | UI disabled; API `hibernate_unsupported` |
| Save disk full | Error; no `hibernated` state |
| Mixed VM states | Start resumes each by its state; Off applies mode only to currently running VMs |
| Selection Off | Only selected VM ids |
| VM/project destroy | managedsave artifacts removed with domain |

## Testing

- Unit: metering (`paused` bills compute; `hibernated` does not); start-cascade state machine; capability flags.
- API tests with mocked troshkad / KubeVirt (no real I/O).
- troshkad tests for pause/resume/hibernate handlers where feasible.
- Frontend: mode picker persistence, KubeVirt hibernate disabled, confirm + dismiss flag.

## Implementation approach

Mirror existing start/stop routes and troshkad jobs (not a single mega power RPC). Frontend wires the split Off control to extended project stop/start plus new per-VM pause/hibernate endpoints.

## Open follow-ups (post-v1)

- Wire KubeVirt hibernate when upstream ships a stable API.
- Optional meter line for save-image storage.
- Deeper save-image disk-pressure warnings in the UI before hibernate.
