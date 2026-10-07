# VM Pause and Hibernate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add project- and per-VM Pause/Hibernate with a split Off control, correct metering (pause bills compute; hibernate does not), and KubeVirt hibernate disabled until upstream lands.

**Architecture:** Extend existing start/stop routes and troshkad job handlers (not a mega power RPC). New project columns `off_action` / `power_warn_dismissed`. troshkad gains pause/resume/hibernate; KubeVirt uses VMI pause subresources. Project `POST .../stop` honors `off_action`; `POST .../start` resumes by live state.

**Tech Stack:** FastAPI, SQLAlchemy 2, Alembic, troshkad/libvirt, KubeVirt subresources, Next.js 15, RQ jobs, WS `vm-state`

**Spec:** `docs/superpowers/specs/2026-10-07-vm-pause-hibernate-design.md`

## Global Constraints

- Field name **`off_action`** (`stop`|`pause`|`hibernate`), not `power_off_mode` — existing `poweroff_mode` is sequential/parallel/simultaneous stop *ordering*
- Default `off_action=stop`; default `power_warn_dismissed=false`
- Pause bills vCPU/RAM; hibernate does not; disks/EIPs/Ceph unchanged
- KubeVirt: pause yes; hibernate → `code: hibernate_unsupported` (never map to Halted)
- Cognitive complexity ≤ 15 per function; extract helpers
- Tests: `cd src/backend && ./venv/bin/python3 -m pytest …`; mock all troshkad/K8s I/O
- Git add with absolute paths from repo root; no AI attribution in commits
- Confirm copy for pause/hibernate per spec; “Don’t show again for this project”

## File map

| Path | Role |
| --- | --- |
| `src/backend/app/models/project.py` | `off_action`, `power_warn_dismissed` |
| `src/backend/app/schemas/project.py` | request/response fields |
| `src/backend/alembic/versions/<rev>_add_project_off_action.py` | migration; `down_revision=d9e0f1a2b3c4` |
| `src/backend/app/services/metering_math.py` | `paused` running; `hibernated` stopped |
| `src/backend/app/services/vm_power.py` | **new** shared helpers: capability, cascade off/on, kubevirt pause |
| `src/backend/app/api/projects.py` | per-VM pause/unpause/hibernate; extend stop/start; PATCH validation |
| `src/backend/app/services/deploy_service.py` | `stop_project_async` / start path honor `off_action` |
| `src/troshkad/troshkad.py` | `/vms/pause`, `/vms/resume`, `/vms/hibernate` handlers |
| `src/backend/app/services/ws_pubsub.py` | map hibernated in state tokens if needed |
| `src/frontend/src/app/projects/[id]/page.tsx` | split Off + Start; confirm + dismiss |
| `src/frontend/src/components/canvas/nodes/VMNode.tsx` | pause/hibernate actions |
| `src/frontend/src/components/canvas/NodeContextMenu.tsx` | menu entries |
| `src/frontend/src/components/canvas/Canvas.tsx` | multi-select Off/Start |
| `src/backend/tests/test_metering_math.py` | paused/hibernated cases |
| `src/backend/tests/test_vm_power.py` | **new** capability + cascade unit tests |
| `src/backend/tests/test_api_projects.py` | API validation / unsupported hibernate |

---

### Task 1: Project columns and schemas

**Files:**
- Modify: `src/backend/app/models/project.py` (after `poweroff_mode`)
- Modify: `src/backend/app/schemas/project.py`
- Modify: `src/backend/app/api/projects.py` (`_project_to_dict` / create / update)
- Create: `src/backend/alembic/versions/e1a2b3c4d5e6_add_project_off_action.py` (use `alembic revision` for real rev id; head is `d9e0f1a2b3c4`)

**Interfaces:**
- Produces: `Project.off_action: str` default `"stop"`; `Project.power_warn_dismissed: bool` default `False`
- API JSON keys: `off_action`, `power_warn_dismissed`
- Valid `off_action`: `stop`, `pause`, `hibernate`

- [ ] **Step 1: Write failing schema/API test**

```python
def test_update_project_off_action():
    # create project via existing helper, then:
    resp = client.patch(
        f"/api/v1/projects/{pid}",
        json={"off_action": "pause", "power_warn_dismissed": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["off_action"] == "pause"
    assert body["power_warn_dismissed"] is True
```

- [ ] **Step 2: Run test — expect FAIL** (field missing)

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_api_projects.py::test_update_project_off_action -v`

- [ ] **Step 3: Add model columns, Alembic migration, schema + serialize/PATCH**

```python
# project.py
off_action: Mapped[str] = mapped_column(String(20), default="stop")
power_warn_dismissed: Mapped[bool] = mapped_column(default=False, server_default="false")
```

On PATCH: if `off_action` not in allowed set → 422. Do **not** yet enforce KubeVirt hibernate reject (Task 5).

- [ ] **Step 4: Run test — expect PASS**

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/models/project.py src/backend/app/schemas/project.py src/backend/app/api/projects.py src/backend/alembic/versions/*off_action*.py src/backend/tests/test_api_projects.py && git commit -m "$(cat <<'EOF'
Add project off_action and power_warn_dismissed fields.

EOF
)"
```

---

### Task 2: Metering — paused bills, hibernated does not (TDD)

**Files:**
- Modify: `src/backend/app/services/metering_math.py`
- Modify: `src/backend/tests/test_metering_math.py`

**Interfaces:**
- Consumes: `desired_resources`, `_STOPPED_STATES`, `_RUNNING_STATES`, `_vm_running`
- Produces: `paused` ∈ running for compute; `hibernated` ∈ stopped for compute

- [ ] **Step 1: Failing tests**

```python
def test_desired_paused_vm_still_bills_cpu_ram():
    got = desired_resources(
        {
            "provider_type": "libvirt",
            "host_id": "h1",
            "project_state": "active",
            "vms": [
                {
                    "id": "vm1",
                    "vcpus": 4,
                    "ram_gib": 8,
                    "live_state": "paused",
                }
            ],
            "disks": [{"id": "d1", "size_gib": 80, "host_id": "h1"}],
        }
    )
    assert ("vcpu", "vm1", 4.0, "h1", "libvirt") in got
    assert ("ram", "vm1", 8.0, "h1", "libvirt") in got
    assert ("disk", "d1", 80.0, "h1", "libvirt") in got


def test_desired_hibernated_vm_skips_cpu_ram_keeps_disk():
    got = desired_resources(
        {
            "provider_type": "libvirt",
            "host_id": "h1",
            "project_state": "active",
            "vms": [
                {
                    "id": "vm1",
                    "vcpus": 4,
                    "ram_gib": 8,
                    "live_state": "hibernated",
                }
            ],
            "disks": [{"id": "d1", "size_gib": 80, "host_id": "h1"}],
        }
    )
    kinds = {row[0] for row in got if row[1] == "vm1"}
    assert "vcpu" not in kinds and "ram" not in kinds
    assert ("disk", "d1", 80.0, "h1", "libvirt") in got
```

- [ ] **Step 2: Run — expect FAIL** (`paused` currently in `_STOPPED_STATES`)

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_metering_math.py::test_desired_paused_vm_still_bills_cpu_ram tests/test_metering_math.py::test_desired_hibernated_vm_skips_cpu_ram_keeps_disk -v`

- [ ] **Step 3: Minimal fix**

```python
_RUNNING_STATES = frozenset({"running", "active", "on", "paused"})
_STOPPED_STATES = frozenset(
    {"stopped", "shutoff", "off", "halted", "hibernated", "shut_off"}
)
# remove "paused" from _STOPPED_STATES
```

- [ ] **Step 4: Run full metering math suite — PASS**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_metering_math.py -v`

- [ ] **Step 5: Commit** `Fix metering so paused VMs still accrue compute.`

---

### Task 3: troshkad pause / resume / hibernate

**Files:**
- Modify: `src/troshkad/troshkad.py` (near `_handle_vm_stop` ~1871)
- Test: add or extend troshkad unit tests if a harness exists; otherwise backend mocks of `start_job` cover integration

**Interfaces:**
- Produces command handlers:
  - `vms/pause` → `virsh suspend <domain>` → `{"domain", "status": "paused"}`
  - `vms/resume` → `virsh resume <domain>` → `{"domain", "status": "running"}`
  - `vms/hibernate` → `virsh managedsave <domain>` → `{"domain", "status": "hibernated"}`
- Params: `{"domain_name": str}` (same validation as stop via `_validate_domain_name`)
- Domstate mapping: ensure `"paused"` and post-managedsave shut-off report as `hibernated` when a managedsave image exists (`virsh dominfo` / `managedsave-info` if available; else treat shutoff+managedsave file as hibernated in list/state helper)

- [ ] **Step 1: Implement three handlers** next to `COMMAND_HANDLERS["vms/stop"]`

```python
def _handle_vm_pause(job, params):
    domain = _validate_domain_name(params["domain_name"])
    _run_cmd(job, ["virsh", "suspend", domain], timeout=30)
    return {"domain": domain, "status": "paused"}

COMMAND_HANDLERS["vms/pause"] = _handle_vm_pause

def _handle_vm_resume(job, params):
    domain = _validate_domain_name(params["domain_name"])
    _run_cmd(job, ["virsh", "resume", domain], timeout=30)
    return {"domain": domain, "status": "running"}

COMMAND_HANDLERS["vms/resume"] = _handle_vm_resume

def _handle_vm_hibernate(job, params):
    domain = _validate_domain_name(params["domain_name"])
    _run_cmd(job, ["virsh", "managedsave", domain], timeout=600)
    return {"domain": domain, "status": "hibernated"}

COMMAND_HANDLERS["vms/hibernate"] = _handle_vm_hibernate
```

- [ ] **Step 2: State reporting** — in `_handle_vm_state` / list mapping, if domain is shut off and managedsave exists, return `hibernated` not `shut_off`. Prefer a small helper `_domain_power_state(domain) -> str` to keep complexity ≤ 15.

- [ ] **Step 3: Smoke** (optional on a lab host): pause → resume; hibernate → `virsh start` restores. Document in commit message if not automated.

- [ ] **Step 4: Commit** `Add troshkad pause, resume, and managedsave hibernate.`

---

### Task 4: Shared `vm_power` helpers + per-VM API

**Files:**
- Create: `src/backend/app/services/vm_power.py`
- Modify: `src/backend/app/api/projects.py` (new routes near `stop_vm` ~2730)
- Create: `src/backend/tests/test_vm_power.py`
- Modify: `src/backend/tests/test_api_projects.py`

**Interfaces:**
- Produces:
  - `supports_hibernate(host) -> bool` — `True` iff `host.host_type != "kubevirt-cluster"`
  - `pause_vm_on_host(host, project_id, vm_id) -> None`
  - `unpause_vm_on_host(host, project_id, vm_id) -> None`
  - `hibernate_vm_on_host(host, project_id, vm_id) -> None` — raises `HibernateUnsupported` on KubeVirt
  - `HibernateUnsupported` exception with `.code = "hibernate_unsupported"`
- KubeVirt pause/unpause: PUT `.../virtualmachineinstances/{kv_name}/pause|unpause` with empty body / `Accept: */*` (copy pattern from `ocpvirt.unpause_host`)
- troshkad: `start_job(host, "/vms/pause"|"/vms/resume"|"/vms/hibernate", {"domain_name": dom})` + `wait_for_job`
- Routes:
  - `POST /projects/{id}/vms/{vm_id}/pause`
  - `POST /projects/{id}/vms/{vm_id}/unpause`
  - `POST /projects/{id}/vms/{vm_id}/hibernate`
- Each notifies WS `vm-state` and `_touch_metering`
- Extend `start_vm`: if cached/live state is `paused`, call unpause path before/instead of cold start

- [ ] **Step 1: Unit tests for `supports_hibernate` and exception**

```python
def test_supports_hibernate_false_for_kubevirt():
    host = SimpleNamespace(host_type="kubevirt-cluster")
    assert supports_hibernate(host) is False

def test_hibernate_kubevirt_raises():
    with pytest.raises(HibernateUnsupported):
        hibernate_vm_on_host(SimpleNamespace(host_type="kubevirt-cluster"), "p", "v")
```

- [ ] **Step 2: Implement `vm_power.py` + routes** (mock `start_job` / k8s in API tests)

- [ ] **Step 3: API test hibernate on kubevirt → 501/409 with `detail.code == "hibernate_unsupported"`** (match existing HTTPException detail style in this codebase)

- [ ] **Step 4: Commit** `Add per-VM pause, unpause, and hibernate APIs.`

---

### Task 5: Project stop/start honor `off_action`

**Files:**
- Modify: `src/backend/app/services/deploy_service.py` (`stop_project_async`, start counterpart)
- Modify: `src/backend/app/api/projects.py` (`stop_project`, `start_project`, PATCH validation)
- Modify: `src/backend/tests/test_project_helpers.py` or new tests with mocks

**Interfaces:**
- `stop_project` / `stop_project_async`:
  - Read `project.off_action`
  - If `hibernate` and not `supports_hibernate(host)` → set error / fail fast with `hibernate_unsupported` (do not leave project stuck in `stopping`)
  - `stop`: existing graceful stop path
  - `pause`: pause each running VM; **project.state stays `active`** (do not set `stopped`); notify per-VM `paused`
  - `hibernate`: hibernate each running VM; when all are non-running and none paused, set `project.state = "stopped"` (same end state as stop for Start button UX)
- `start_project` / start async: for each VM, if paused → unpause; if hibernated/stopped → start (libvirt restores managedsave on start)
- Optional body later: `{ "vm_ids": [...] }` — implement if cheap; else all-VMs only in this task and selection uses per-VM endpoints
- PATCH: if host is kubevirt and `off_action == "hibernate"` → 409 `hibernate_unsupported`

- [ ] **Step 1: Failing test** — mocked stop with `off_action=pause` does not set project `stopped`

- [ ] **Step 2: Implement branch helpers** `_apply_off_action_troshkad` / `_apply_off_action_kubevirt` (complexity)

- [ ] **Step 3: Run targeted deploy/project tests**

- [ ] **Step 4: Commit** `Honor off_action in project stop and start cascades.`

---

### Task 6: Capability on project/host payload

**Files:**
- Modify: `src/backend/app/api/projects.py` (`_project_to_dict`)
- Modify: frontend types if any

**Interfaces:**
- Project GET includes `supports_hibernate: bool` derived from host via `supports_hibernate(host)`
- Keep `off_action` / `power_warn_dismissed` in the same payload

- [ ] **Step 1: Assert in API test** kubevirt project → `supports_hibernate is False`

- [ ] **Step 2: Wire serialize**

- [ ] **Step 3: Commit** `Expose supports_hibernate on project responses.`

---

### Task 7: Frontend project toolbar (split Off control)

**Files:**
- Modify: `src/frontend/src/app/projects/[id]/page.tsx` (~1477–1536 Stop/Start)
- Reuse existing `appConfirm`

**Interfaces:**
- Consumes: `off_action`, `power_warn_dismissed`, `supports_hibernate` from project fetch
- Off primary label: Stop / Pause / Hibernate
- ▾ menu PATCHes `off_action` (disable Hibernate when `!supports_hibernate`)
- Off click:
  1. If `off_action` in `pause|hibernate` and `!power_warn_dismissed` → confirm with spec copy; if “don’t show again” checked → PATCH `power_warn_dismissed: true`
  2. `POST /api/v1/projects/${id}/stop`
- Start: existing `POST .../start`
- After pause, project may remain `active` — keep Off+Start visible when any VM is paused/running; when `stopped`, show Start as today

- [ ] **Step 1: Implement split control UI** (PatternFly/dropdown consistent with nearby buttons; no new design system)

Confirm message (pause):

```
This freezes the guest without a clean service shutdown. Databases, clusters, and network services may not recover cleanly when resumed. Mileage may vary.
```

Hibernate adds second paragraph about RAM freed + save image.

- [ ] **Step 2: Manual check** in browser (dev): mode picker PATCH; KubeVirt greyed hibernate

- [ ] **Step 3: Commit** `Add project Off mode split control for pause and hibernate.`

---

### Task 8: Frontend per-VM + context menu + multi-select

**Files:**
- Modify: `src/frontend/src/components/canvas/nodes/VMNode.tsx`
- Modify: `src/frontend/src/components/canvas/NodeContextMenu.tsx`
- Modify: `src/frontend/src/components/canvas/Canvas.tsx` (selection toolbar ~1025)
- Status badge mapping wherever VM state labels are rendered (canvas + project page)

**Interfaces:**
- `vmAction` union extends `"pause" | "unpause" | "hibernate"`
- Routes: `/pause`, `/unpause`, `/hibernate`
- Context menu when running: Graceful Shutdown, Pause, Hibernate (disabled if `!supportsHibernate`), Force Off, Restart
- When paused: show Resume (unpause or start) prominently
- When hibernated: show Start
- Badges: do not map `paused` → “Stopped”
- Multi-select: Off uses project `off_action` + confirm; or explicit Pause/Hibernate if easier — match project mode for consistency

- [ ] **Step 1: Wire actions + confirms** (same dismiss flag via project PATCH)

- [ ] **Step 2: Fix status label mapping** for `paused` / `hibernated`

- [ ] **Step 3: Commit** `Expose pause and hibernate on VM nodes and menus.`

---

### Task 9: WS / state polish + regression pass

**Files:**
- Modify: `src/backend/app/services/ws_pubsub.py` if token sets need `hibernated`
- Grep frontend/backend for `shut_off` / `paused` maps and align

- [ ] **Step 1: Grep and fix missed state labels**

- [ ] **Step 2: Run**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_metering_math.py tests/test_vm_power.py tests/test_api_projects.py -v --timeout=60
```

- [ ] **Step 3: Commit** `Align WS and UI state labels for pause and hibernate.`

---

## Spec coverage checklist

| Spec requirement | Task |
| --- | --- |
| `off_action` / warn dismiss fields | 1 |
| Metering pause on / hibernate off | 2 |
| troshkad suspend/resume/managedsave | 3 |
| Per-VM pause/unpause/hibernate + KV pause | 4 |
| Project stop/start cascade | 5 |
| `supports_hibernate` | 6 |
| Toolbar B + confirm | 7 |
| Per-VM / selection UX | 8 |
| State chrome / WS | 9 |
| No KubeVirt hibernate | 4, 5, 6, 7 |
| Naming vs `poweroff_mode` | Global + Task 1 |

## Naming note

Use **`off_action`** (not `power_off_mode`) to avoid colliding with existing **`poweroff_mode`** (stop ordering). Spec updated to match. Enum: `stop` | `pause` | `hibernate`.
