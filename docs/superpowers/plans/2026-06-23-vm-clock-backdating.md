# VM Clock Backdating Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow project VMs to run with clocks set to a specific past or future date, with gateway-provided NTP and live adjustment support.

**Architecture:** A `clock_target` DateTime column on the Project model drives a libvirt `--clock offset=variable,adjustment=N` flag at VM creation. Every project's gateway runs chrony as a local NTP server; all VMs sync exclusively from the gateway. Live adjustment updates libvirt XML and pushes time via guest-agent (fallback: exec).

**Tech Stack:** Python (FastAPI, SQLAlchemy, Alembic), libvirt/virt-install, chrony, Next.js/React

## Global Constraints

- Python 3.11, use `python3` not `python`
- SQLAlchemy 2.0+ `Mapped[]` + `mapped_column()` syntax
- UUIDs as strings: `UUID(as_uuid=False)`
- FK columns must use `postgresql.UUID(as_uuid=False)` (not `String(36)`)
- Troshkad is stdlib-only Python — no pip packages
- Backend has no auto-reload — Python changes require manual restart
- Never use `sed` for file edits
- Run `black` before committing

---

### Task 1: Database — Add `clock_target` column

**Files:**
- Modify: `src/backend/app/models/project.py:59` (add column before `created_at`)
- Create: `src/backend/alembic/versions/<auto>_add_clock_target.py`

**Interfaces:**
- Produces: `Project.clock_target` — `Mapped[datetime.datetime | None]`, `DateTime(timezone=True)`, nullable

- [ ] **Step 1: Add column to Project model**

In `src/backend/app/models/project.py`, add after `dns_provider_id` (line 59) and before `created_at` (line 60):

```python
clock_target: Mapped[datetime.datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
)
```

- [ ] **Step 2: Generate Alembic migration**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m alembic revision -m "add clock_target to projects"
```

Edit the generated migration file. The `upgrade()` should be:

```python
def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("clock_target", sa.DateTime(timezone=True), nullable=True),
    )
```

And `downgrade()`:

```python
def downgrade() -> None:
    op.drop_column("projects", "clock_target")
```

- [ ] **Step 3: Run migration**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m alembic upgrade head
```

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/models/project.py src/backend/alembic/versions/*clock_target*
git commit -m "feat: add clock_target column to projects table"
```

---

### Task 2: API — Schema, PATCH, Response, and live adjustment trigger

**Files:**
- Modify: `src/backend/app/schemas/project.py:16-28` (ProjectUpdate) and `:30-59` (ProjectResponse)
- Modify: `src/backend/app/api/projects.py:613-670` (PATCH endpoint)
- Modify: `src/backend/app/api/projects.py:532-578` (export endpoint)
- Modify: `src/backend/app/api/projects.py:199-398` (from-template endpoint)
- Create: `src/backend/app/services/clock_service.py`

**Interfaces:**
- Consumes: `Project.clock_target` from Task 1
- Produces: `ProjectUpdate.clock_target` — `datetime.datetime | None`
- Produces: `ProjectResponse.clock_target` — `datetime.datetime | None`
- Produces: `adjust_clocks_async(project_id: str)` — background thread for live adjustment
- Produces: `compute_clock_offset(clock_target: datetime.datetime) -> int` — returns seconds offset

- [ ] **Step 1: Write test for clock_target in PATCH**

Add to `src/backend/tests/test_projects.py`:

```python
def test_set_clock_target():
    list_resp = client.get("/api/v1/projects", headers=HEADERS)
    project_id = list_resp.json()[0]["id"]
    resp = client.patch(
        f"/api/v1/projects/{project_id}",
        json={"clock_target": "2025-01-15T00:00:00Z"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["clock_target"] is not None
    assert "2025-01-15" in data["clock_target"]


def test_clear_clock_target():
    list_resp = client.get("/api/v1/projects", headers=HEADERS)
    project_id = list_resp.json()[0]["id"]
    resp = client.patch(
        f"/api/v1/projects/{project_id}",
        json={"clock_target": None},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["clock_target"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_projects.py::test_set_clock_target tests/test_projects.py::test_clear_clock_target -v
```

Expected: FAIL — `clock_target` not in ProjectUpdate/ProjectResponse schemas.

- [ ] **Step 3: Update schemas**

In `src/backend/app/schemas/project.py`:

Add to `ProjectUpdate` (after `guid` on line 27):

```python
clock_target: datetime.datetime | None = None
```

Add to `ProjectResponse` (after `guid` on line 55):

```python
clock_target: datetime.datetime | None = None
```

- [ ] **Step 4: Create clock_service.py**

Create `src/backend/app/services/clock_service.py`:

```python
"""Clock backdating service — compute offsets and push time to running VMs."""

import datetime
import logging
import threading

logger = logging.getLogger(__name__)


def compute_clock_offset(clock_target: datetime.datetime) -> int:
    """Compute seconds offset from current UTC to the target datetime."""
    now = datetime.datetime.now(datetime.timezone.utc)
    if clock_target.tzinfo is None:
        clock_target = clock_target.replace(tzinfo=datetime.timezone.utc)
    return int((clock_target - now).total_seconds())


def adjust_clocks_async(project_id: str):
    """Background thread: push new clock_target to all running VMs."""
    t = threading.Thread(
        target=_adjust_clocks, args=(project_id,), daemon=True
    )
    t.start()


def _adjust_clocks(project_id: str):
    """Push updated clock to all VMs in a project."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project
    from app.services.troshkad_client import start_job, wait_for_job

    s = SessionLocal()
    try:
        project = s.query(Project).filter_by(id=project_id).first()
        if not project or project.state != "active":
            return

        host = s.query(Host).filter_by(id=project.host_id).first() if project.host_id else None
        if not host:
            return

        topology = project.deployed_topology or project.topology or {}
        nodes = topology.get("nodes", [])
        vm_nodes = [n for n in nodes if n.get("type") == "vmNode"]

        if project.clock_target:
            offset_seconds = compute_clock_offset(project.clock_target)
            target_epoch = int(project.clock_target.timestamp())
        else:
            offset_seconds = None
            target_epoch = None

        # Process gateway first (it's the NTP server), then other VMs
        gateway_nodes = [n for n in nodes if n.get("type") == "gatewayNode"]
        gateway_domain = None
        if gateway_nodes:
            gw = gateway_nodes[0]
            gateway_domain = f"troshka-{project_id[:8]}-{gw['id'][:8]}"
            _push_clock_to_vm(host, gateway_domain, offset_seconds, target_epoch)

        for node in vm_nodes:
            domain = f"troshka-{project_id[:8]}-{node['id'][:8]}"
            if domain == gateway_domain:
                continue
            _push_clock_to_vm(host, domain, offset_seconds, target_epoch)

        logger.info(
            "Clock adjustment complete for project %s (%s VMs)",
            project_id[:8],
            len(vm_nodes),
        )
    except Exception:
        logger.exception("Clock adjustment failed for project %s", project_id[:8])
    finally:
        s.close()


def _push_clock_to_vm(host, domain_name, offset_seconds, target_epoch):
    """Update a single VM's clock: XML + live push."""
    from app.services.troshkad_client import TroshkadError, start_job, wait_for_job

    try:
        job_id = start_job(
            host,
            "/vms/set-clock",
            {
                "domain_name": domain_name,
                "offset_seconds": offset_seconds,
                "target_epoch": target_epoch,
            },
        )
        wait_for_job(host, job_id, timeout=30)
    except TroshkadError as e:
        logger.warning("Clock push failed for %s: %s", domain_name, e)
```

- [ ] **Step 5: Add live adjustment trigger to PATCH endpoint**

In `src/backend/app/api/projects.py`, after the auto-delete timer block (after line 662), add:

```python
# Live clock adjustment
if "clock_target" in fields and project.state == "active":
    from app.services.clock_service import adjust_clocks_async

    adjust_clocks_async(project_id)
```

- [ ] **Step 6: Add clock_target to export endpoint**

In `src/backend/app/api/projects.py`, in the `export_template` function, after `result["description"]` (line 551), add:

```python
if project.clock_target:
    result["clock_target"] = project.clock_target.isoformat()
```

- [ ] **Step 7: Add clock_target to from-template endpoint**

In `src/backend/app/api/projects.py`, in the `create_project_from_template` function, after the Project() constructor (line 389-394), add clock_target from resolved template or body:

```python
clock_target_str = body.get("clock_target") or resolved.get("clock_target")
if clock_target_str:
    from datetime import datetime, timezone

    if isinstance(clock_target_str, str):
        ct = datetime.fromisoformat(clock_target_str.replace("Z", "+00:00"))
    else:
        ct = clock_target_str
    project.clock_target = ct
```

- [ ] **Step 8: Run tests to verify they pass**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_projects.py::test_set_clock_target tests/test_projects.py::test_clear_clock_target -v
```

Expected: PASS

- [ ] **Step 9: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/schemas/project.py src/backend/app/api/projects.py src/backend/app/services/clock_service.py src/backend/tests/test_projects.py
git add src/backend/app/schemas/project.py src/backend/app/api/projects.py src/backend/app/services/clock_service.py src/backend/tests/test_projects.py
git commit -m "feat: clock_target API — schema, PATCH, export, from-template, live adjustment service"
```

---

### Task 3: Template loader — import and resolve `clock_target`

**Files:**
- Modify: `src/backend/app/services/template_loader.py:97-141` (resolve_inline_template)
- Modify: `src/backend/app/api/projects.py:401-450` (import-template endpoint)

**Interfaces:**
- Consumes: `Project.clock_target` from Task 1
- Produces: `resolved["clock_target"]` — passed through from template YAML

- [ ] **Step 1: Write test for clock_target in template resolution**

Add to `src/backend/tests/test_template_loader.py`:

```python
def test_resolve_inline_clock_target():
    from app.services.template_loader import resolve_inline_template

    tmpl = {
        "name": "test-clock",
        "clock_target": "2025-01-15T00:00:00Z",
        "networks": {"net1": {"cidr": "192.168.1.0/24"}},
        "vms": {"vm1": {"vcpus": 2, "ram_gb": 4, "os": "rhel-9"}},
    }
    resolved = resolve_inline_template(tmpl)
    assert resolved["clock_target"] == "2025-01-15T00:00:00Z"


def test_resolve_inline_no_clock_target():
    from app.services.template_loader import resolve_inline_template

    tmpl = {
        "name": "test-no-clock",
        "networks": {"net1": {"cidr": "192.168.1.0/24"}},
        "vms": {"vm1": {"vcpus": 2, "ram_gb": 4, "os": "rhel-9"}},
    }
    resolved = resolve_inline_template(tmpl)
    assert resolved.get("clock_target") is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_template_loader.py::test_resolve_inline_clock_target tests/test_template_loader.py::test_resolve_inline_no_clock_target -v
```

Expected: FAIL — `clock_target` not passed through.

- [ ] **Step 3: Add clock_target to resolve_inline_template**

In `src/backend/app/services/template_loader.py`, in `resolve_inline_template()`, add `"clock_target"` to the section loop (line 129-139):

```python
for section in (
    "ocp",
    "dns_records",
    "disconnected",
    "bastion_services",
    "start_order",
    "hidden_nodes",
    "pull_through_registry",
    "clock_target",
):
    if tmpl.get(section):
        resolved[section] = tmpl[section]
```

- [ ] **Step 4: Add clock_target handling to import-template endpoint**

In `src/backend/app/api/projects.py`, in the `import_template` function, after the topology is set on the project, add:

```python
clock_target_str = resolved.get("clock_target")
if clock_target_str:
    from datetime import datetime

    if isinstance(clock_target_str, str):
        ct = datetime.fromisoformat(clock_target_str.replace("Z", "+00:00"))
    else:
        ct = clock_target_str
    project.clock_target = ct
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_template_loader.py::test_resolve_inline_clock_target tests/test_template_loader.py::test_resolve_inline_no_clock_target -v
```

Expected: PASS

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/template_loader.py src/backend/app/api/projects.py src/backend/tests/test_template_loader.py
git add src/backend/app/services/template_loader.py src/backend/app/api/projects.py src/backend/tests/test_template_loader.py
git commit -m "feat: clock_target in template import/resolve"
```

---

### Task 4: Deploy pipeline — pass clock_offset to troshkad

**Files:**
- Modify: `src/backend/app/services/deploy_service.py:1311-1395` (_create_vm_via_troshkad)
- Modify: `src/backend/app/services/deploy_service.py:2224` (call site in deploy_project_async)

**Interfaces:**
- Consumes: `Project.clock_target` from Task 1, `compute_clock_offset()` from Task 2
- Produces: `params["clock_offset"]` — integer seconds, passed to troshkad `/vms/create`

- [ ] **Step 1: Add clock_offset parameter to _create_vm_via_troshkad**

In `src/backend/app/services/deploy_service.py`, update the function signature at line 1311:

```python
def _create_vm_via_troshkad(
    host, project_id, vm, topology, vni_map, pool=None, disk_cache=None, clock_offset=None
):
```

After the `disk_cache` conditional (line 1391-1392), add:

```python
if clock_offset is not None:
    params["clock_offset"] = clock_offset
```

- [ ] **Step 2: Pass clock_offset from deploy_project_async**

In `deploy_project_async`, after `topology` and `vni_map` are read from the project (around line 1742-1743), compute the offset:

```python
clock_offset = None
if project.clock_target:
    from app.services.clock_service import compute_clock_offset

    clock_offset = compute_clock_offset(project.clock_target)
```

Then at the call site (line 2224-2225), pass it:

```python
job_id = _create_vm_via_troshkad(
    host, project_id, vm, topology, vni_map, pool, disk_cache, clock_offset
)
```

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/deploy_service.py
git add src/backend/app/services/deploy_service.py
git commit -m "feat: pass clock_offset to troshkad during VM creation"
```

---

### Task 5: Cloud-init — NTP client config pointing at gateway

**Files:**
- Modify: `src/backend/app/services/cloud_init.py:21-155` (generate_userdata)
- Modify: `src/backend/app/services/deploy_service.py` (inject gateway_ip into VM data)

**Interfaces:**
- Consumes: `vm_data["gateway_ip"]` — string, the gateway's bridge IP (first usable address in CIDR)
- Produces: chrony client configuration in cloud-init user-data for all VMs

Every VM's chrony points at the gateway IP. The gateway namespace runs its own chronyd instance (Task 6). VMs never use public NTP pools.

- [ ] **Step 1: Write test for NTP client cloud-init**

Create `src/backend/tests/test_cloud_init.py`:

```python
from app.services.cloud_init import generate_userdata


def test_vm_gets_chrony_client_config():
    vm_data = {
        "name": "bastion",
        "cloudInit": True,
        "gateway_ip": "192.168.1.1",
    }
    userdata = generate_userdata(vm_data)
    assert "chrony" in userdata
    assert "server 192.168.1.1 iburst prefer" in userdata
    assert "makestep 1 -1" in userdata


def test_vm_without_gateway_ip_no_chrony_override():
    vm_data = {
        "name": "standalone",
        "cloudInit": True,
    }
    userdata = generate_userdata(vm_data)
    assert "makestep 1 -1" not in userdata
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_cloud_init.py -v
```

Expected: FAIL — no chrony config generated.

- [ ] **Step 3: Add chrony NTP client config to generate_userdata**

In `src/backend/app/services/cloud_init.py`, in `generate_userdata()`, after the packages section (after line 94) and before the custom user-data section (line 96), add:

```python
gateway_ip = vm_data.get("gateway_ip")
chrony_runcmd_lines = []
if gateway_ip:
    if "chrony" not in all_packages:
        all_packages.append("chrony")
    chrony_runcmd_lines.append(
        f'  - printf "server {gateway_ip} iburst prefer\\nmakestep 1 -1\\ndriftfile /var/lib/chrony/drift\\n" > /etc/chrony.conf'
    )
    chrony_runcmd_lines.append("  - systemctl restart chronyd 2>/dev/null || true")
```

Then in the runcmd block (before `lines.extend(custom_runcmd_lines)` on line 124), add:

```python
lines.extend(chrony_runcmd_lines)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_cloud_init.py -v
```

Expected: PASS

- [ ] **Step 5: Inject gateway_ip into VM data in deploy_service**

In `src/backend/app/services/deploy_service.py`, in `deploy_project_async`, after networks are set up but before seed ISOs are created, find the gateway IP from the topology and inject it into each VM's cloud-init data:

```python
# Find gateway IP for NTP — first usable address in first network's CIDR
gateway_ip = None
for node in topology.get("nodes", []):
    if node.get("type") == "gatewayNode":
        for edge in topology.get("edges", []):
            if edge.get("source") == node["id"]:
                target_node = next(
                    (n for n in topology["nodes"] if n["id"] == edge["target"]),
                    None,
                )
                if target_node and target_node.get("type") == "networkNode":
                    net_data = target_node.get("data", {})
                    cidr = net_data.get("cidr", "192.168.1.0/24")
                    import ipaddress
                    network = ipaddress.ip_network(cidr, strict=False)
                    gateway_ip = str(network.network_address + 1)
                    break
        break

if gateway_ip:
    for node in topology.get("nodes", []):
        if node.get("type") == "vmNode" and node.get("data", {}).get("cloudInit"):
            node["data"]["gateway_ip"] = gateway_ip
```

- [ ] **Step 6: Commit**

```bash
cd /Users/prutledg/troshka && black src/backend/app/services/cloud_init.py src/backend/app/services/deploy_service.py src/backend/tests/test_cloud_init.py
git add src/backend/app/services/cloud_init.py src/backend/app/services/deploy_service.py src/backend/tests/test_cloud_init.py
git commit -m "feat: chrony NTP client config in cloud-init, pointing at gateway"
```

---

### Task 6: Troshkad — clock_offset in virt-install, gateway chrony, and /vms/set-clock

**Files:**
- Modify: `src/troshkad/troshkad.py:783-890` (_handle_vm_create — add --clock flag)
- Modify: `src/troshkad/troshkad.py:2410-2538` (_handle_network_full_setup — add chrony after dnsmasq)
- Modify: `src/troshkad/troshkad.py:3316-3390` (_handle_network_full_teardown — kill chrony)
- Modify: `src/troshkad/troshkad.py` (add _handle_vm_set_clock + handler registration)

**Interfaces:**
- Consumes: `params["clock_offset"]` — integer seconds, from deploy_service (Task 4)
- Consumes: `params["domain_name"]`, `params["offset_seconds"]`, `params["target_epoch"]` — from clock_service (Task 2)
- Produces: troshkad endpoint `POST /vms/set-clock`
- Produces: per-project chronyd running in gateway namespace (always, on every project)

- [ ] **Step 1: Add --clock flag to virt-install**

In `src/troshkad/troshkad.py`, in `_handle_vm_create`, after line 795 (`domain_uuid = params.get("uuid")`), add:

```python
clock_offset = params.get("clock_offset")
```

After the `--channel` line (line 886) and before `_run_cmd(job, cmd, timeout=600)` (line 888), add:

```python
if clock_offset is not None:
    cmd.extend(["--clock", f"offset=variable,adjustment={int(clock_offset)}"])
```

- [ ] **Step 2: Add gateway chrony to /networks/full-setup**

In `src/troshkad/troshkad.py`, in `_handle_network_full_setup`, after the dnsmasq setup loop (after line 2538 `_job_log(job, f"dnsmasq started for VNI {vni} on {bridge}")`), and before the nftables section (line 2540), add the chrony NTP server setup.

The chrony instance runs in the namespace via `ip netns exec`, binding to the first network's gateway IP. This follows the exact same pattern as dnsmasq.

```python
# ── Chrony NTP server inside namespace ──
# Runs on the gateway bridge IP so VMs can sync time from the gateway.
# Uses `local stratum 3` — trusts the host clock (reflects libvirt offset
# when clock_target is set, real time otherwise).
chrony_dir = f"/var/lib/troshka/chrony"
os.makedirs(chrony_dir, exist_ok=True)
chrony_conf = f"{chrony_dir}/{pid}.conf"
chrony_pid = f"/run/troshka-chronyd-{pid}.pid"
chrony_drift = f"{chrony_dir}/{pid}.drift"

# Find the first gateway IP from the networks we just configured
chrony_bind_ip = None
for net in networks:
    dhcp_cfg = net.get("dhcp_config", {})
    gw_ip = dhcp_cfg.get("gateway", "")
    if gw_ip:
        chrony_bind_ip = gw_ip
        break

if chrony_bind_ip:
    conf_content = (
        f"local stratum 3\n"
        f"allow 0.0.0.0/0\n"
        f"driftfile {chrony_drift}\n"
        f"pidfile {chrony_pid}\n"
        f"bindaddress {chrony_bind_ip}\n"
        f"port 123\n"
    )
    with open(chrony_conf, "w") as f:
        f.write(conf_content)

    # Kill existing chronyd for this project
    if os.path.exists(chrony_pid):
        try:
            with open(chrony_pid) as f:
                old_pid = int(f.read().strip())
            _safe_kill(old_pid, signal.SIGTERM)
            for _ in range(10):
                try:
                    os.kill(old_pid, 0)
                    time.sleep(0.25)
                except ProcessLookupError:
                    break
            else:
                _safe_kill(old_pid, signal.SIGKILL)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
        try:
            os.remove(chrony_pid)
        except FileNotFoundError:
            pass

    _run_cmd(
        job,
        ["ip", "netns", "exec", ns, "chronyd", "-f", chrony_conf],
        timeout=10,
    )
    _job_log(job, f"chronyd started on {chrony_bind_ip} in namespace {ns}")
```

- [ ] **Step 3: Add chrony cleanup to /networks/full-teardown**

In `src/troshkad/troshkad.py`, in `_handle_network_full_teardown`, after the dnsmasq cleanup section (after line 3389) and before the metadata service cleanup (line 3391), add:

```python
# Kill chronyd for this project
chrony_pid_file = f"/run/troshka-chronyd-{pid}.pid"
if os.path.exists(chrony_pid_file):
    try:
        with open(chrony_pid_file) as f:
            chrony_pid = int(f.read().strip())
        _safe_kill(chrony_pid, 9)
        _job_log(job, f"Killed chronyd PID {chrony_pid}")
    except (FileNotFoundError, ValueError, ProcessLookupError, OSError):
        pass
    try:
        os.remove(chrony_pid_file)
    except FileNotFoundError:
        pass
# Clean up chrony config and drift files
for chrony_path in [
    f"/var/lib/troshka/chrony/{pid}.conf",
    f"/var/lib/troshka/chrony/{pid}.drift",
]:
    try:
        os.remove(chrony_path)
    except FileNotFoundError:
        pass
```

- [ ] **Step 4: Add /vms/set-clock handler**

In `src/troshkad/troshkad.py`, after `COMMAND_HANDLERS["vms/undefine"]` (line 1532), add:

```python

def _handle_vm_set_clock(job, params):
    """Update a VM's clock offset in libvirt XML and push time to guest."""
    import xml.etree.ElementTree as ET

    domain = _validate_domain_name(params["domain_name"])
    offset_seconds = params.get("offset_seconds")
    target_epoch = params.get("target_epoch")

    # Check if domain is running
    state_result = subprocess.run(
        ["virsh", "domstate", domain],
        capture_output=True,
        text=True,
        timeout=10,
    )
    is_running = state_result.returncode == 0 and "running" in state_result.stdout.lower()

    # Update XML for persistence (on inactive config)
    result = subprocess.run(
        ["virsh", "dumpxml", "--inactive", domain],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get XML for {domain}: {result.stderr}")

    root = ET.fromstring(result.stdout)

    # Find or create <clock> element
    clock_elem = root.find("clock")
    if clock_elem is None:
        clock_elem = ET.SubElement(root, "clock")

    if offset_seconds is not None:
        clock_elem.set("offset", "variable")
        clock_elem.set("adjustment", str(int(offset_seconds)))
        if "basis" in clock_elem.attrib:
            del clock_elem.attrib["basis"]
    else:
        clock_elem.set("offset", "utc")
        for attr in ("adjustment", "basis"):
            if attr in clock_elem.attrib:
                del clock_elem.attrib[attr]

    # Write new XML
    new_xml = ET.tostring(root, encoding="unicode")
    proc = subprocess.Popen(
        ["virsh", "define", "/dev/stdin"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = proc.communicate(input=new_xml, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"virsh define failed: {stderr}")
    _job_log(job, f"Updated clock XML for {domain}")

    # Push time to running VM
    pushed = False
    if is_running and target_epoch is not None:
        # Try guest agent first (virsh domtime uses qemu-guest-agent)
        try:
            ga_result = subprocess.run(
                ["virsh", "domtime", domain, "--set", "--time", str(target_epoch)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if ga_result.returncode == 0:
                pushed = True
                _job_log(job, f"Set time via guest agent on {domain}")
        except (subprocess.TimeoutExpired, Exception):
            pass

        # Fallback: exec date command via guest agent
        if not pushed:
            try:
                exec_result = subprocess.run(
                    [
                        "virsh", "qemu-agent-command", domain,
                        '{"execute":"guest-exec","arguments":{"path":"/usr/bin/date","arg":["-s","@' + str(target_epoch) + '"],"capture-output":true}}',
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if exec_result.returncode == 0:
                    pushed = True
                    _job_log(job, f"Set time via guest-exec on {domain}")
            except (subprocess.TimeoutExpired, Exception):
                _job_log(job, f"Could not push time to {domain} (no guest agent)")
    elif is_running and offset_seconds is None:
        # Clearing clock — push current real time
        import time

        real_epoch = int(time.time())
        try:
            subprocess.run(
                ["virsh", "domtime", domain, "--set", "--time", str(real_epoch)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            pushed = True
            _job_log(job, f"Reset time to real UTC on {domain}")
        except (subprocess.TimeoutExpired, Exception):
            pass

    return {
        "domain": domain,
        "status": "clock_updated",
        "xml_updated": True,
        "time_pushed": pushed,
    }


COMMAND_HANDLERS["vms/set-clock"] = _handle_vm_set_clock
```

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py
git commit -m "feat: troshkad clock offset, gateway chronyd, /vms/set-clock endpoint"
```

---

### Task 7: Frontend — Clock Target in Project Settings (Palette)

**Files:**
- Modify: `src/frontend/src/app/projects/[id]/page.tsx:37-50` (state), `:77-95` (fetch), `:855-876` (Palette props)
- Modify: `src/frontend/src/components/canvas/Palette.tsx:210` (props), `:580-700` (Project section)

**Interfaces:**
- Consumes: `ProjectResponse.clock_target` from Task 2
- Produces: UI for viewing/setting/clearing clock_target via PATCH

- [ ] **Step 1: Add state and fetch for clock_target in page.tsx**

In `src/frontend/src/app/projects/[id]/page.tsx`:

Add state variable near the other project state vars (around line 42):

```typescript
const [clockTarget, setClockTarget] = useState<string | null>(null);
```

In the fetch response handler (where other fields are read from data), add:

```typescript
setClockTarget(data.clock_target ?? null);
```

Add `clockTarget` and `onClockTargetChange` to the Palette component props (around line 855):

```typescript
clockTarget={clockTarget}
onClockTargetChange={(v: string | null) => {
  setClockTarget(v);
  fetch(`/api/v1/projects/${projectId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ clock_target: v }),
  }).then(r => {
    if (r.ok && v !== null && projectState === "active") {
      setToast("Clock updated — VMs syncing");
      setTimeout(() => setToast(null), 3000);
    }
  });
}}
```

- [ ] **Step 2: Add clock_target UI to Palette component**

In `src/frontend/src/components/canvas/Palette.tsx`:

Update the function props type (line 210) to include:

```typescript
clockTarget?: string | null;
onClockTargetChange?: (value: string | null) => void;
```

After the Auto-Delete section (around line 700), add a Clock Target section:

```tsx
{/* Clock Target */}
<div className="palette-item" style={{ cursor: "default" }}>
  <div className="palette-icon" style={{ background: "rgba(147,51,234,0.15)" }}>🕐</div>
  <div style={{ flex: 1 }}>
    <div className="palette-item-label">Clock Target</div>
    <div className="palette-item-desc">Set VM clocks to a specific date</div>
    <div style={{ marginTop: 4, display: "flex", flexDirection: "column", gap: 4 }}>
      <input
        type="datetime-local"
        value={clockTarget ? clockTarget.slice(0, 16) : ""}
        onChange={(e) => {
          const v = e.target.value;
          if (v) {
            onClockTargetChange?.(v + ":00Z");
          }
        }}
        style={{
          fontSize: 10, padding: "2px 4px", borderRadius: 3,
          border: "1px solid var(--pf-t--global--border--color--default)",
          background: "var(--pf-t--global--background--color--secondary--default)",
          color: "var(--pf-t--global--text--color--regular)",
          width: "100%",
        }}
      />
      {clockTarget && (
        <>
          <div style={{ fontSize: 9, opacity: 0.6 }}>
            {(() => {
              const target = new Date(clockTarget);
              const now = new Date();
              const diffMs = now.getTime() - target.getTime();
              const days = Math.floor(Math.abs(diffMs) / 86400000);
              const months = Math.floor(days / 30);
              const remDays = days % 30;
              const label = months > 0 ? `${months}mo ${remDays}d` : `${days}d`;
              return diffMs > 0 ? `${label} behind real time` : `${label} ahead of real time`;
            })()}
          </div>
          <button
            onClick={() => onClockTargetChange?.(null)}
            style={{
              fontSize: 9, padding: "1px 6px", borderRadius: 3,
              border: "1px solid var(--pf-t--global--border--color--default)",
              background: "transparent",
              color: "var(--pf-t--global--text--color--regular)",
              cursor: "pointer", alignSelf: "flex-start",
            }}
          >
            Clear (use real time)
          </button>
        </>
      )}
    </div>
  </div>
</div>
```

- [ ] **Step 3: Test in browser**

Start dev services, open a project, verify:
1. Clock Target section appears in the Palette under Auto-Delete
2. Setting a date sends a PATCH request with `clock_target`
3. Offset indicator shows correct relative time
4. Clear button sends `clock_target: null`
5. On an active project, toast shows "Clock updated — VMs syncing"

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/app/projects/[id]/page.tsx src/frontend/src/components/canvas/Palette.tsx
git commit -m "feat: clock target UI in project palette"
```

---

### Task 8: Final integration test and all-tests pass

**Files:**
- No new files — run existing + new tests

- [ ] **Step 1: Run all backend tests**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/ -v
```

Expected: All tests PASS, including new clock_target tests.

- [ ] **Step 2: Fix any failures**

If any existing tests fail due to the new `clock_target` field in ProjectResponse, update their assertions.

- [ ] **Step 3: Final commit if any fixes needed**

```bash
cd /Users/prutledg/troshka && black src/backend/
git add -u
git commit -m "fix: test adjustments for clock_target feature"
```
