# Pre-Boot Kubelet Cert Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the 10+ minute cert recovery penalty when deploying OCP patterns older than 24 hours, by deleting stale kubelet PKI from RHCOS disks before VM startup.

**Architecture:** New generic `POST /vms/modify-fs` troshkad endpoint uses guestfish to modify guest filesystems offline. Deploy pipeline calls it between VM define and VM start for pattern+OCP deploys, deleting `/var/lib/kubelet/pki` and `/var/lib/kubelet/kubeconfig` from each RHCOS node's boot disk.

**Tech Stack:** guestfish (from `libguestfs-tools`), Python subprocess, existing troshkad job framework

## Global Constraints

- Troshkad is a single-file stdlib-only daemon (`src/troshkad/troshkad.py`) — no pip imports
- Backend tests use SQLite with type compiler overrides — run with `cd src/backend && ./venv/bin/python3 -m pytest tests/ -v`
- Always use `python3` not `python`
- Cert cleanup is **non-fatal** — deploy continues regardless of success/failure
- Only clean RHCOS nodes (`os == "rhcos"`), never bastion (`os == "rhel"`)
- Only pattern deploys (`_is_pattern_deploy`) of OCP topologies (`_is_ocp_topology`)
- Process disks sequentially (guestfish is I/O heavy)
- Always run `black` before committing

---

### Task 1: Add `libguestfs-tools` to Agent Installer

**Files:**
- Modify: `src/backend/app/services/agent_deployer.py:65-66`

**Interfaces:**
- Consumes: nothing
- Produces: `guestfish` binary available on all troshka hosts after agent reinstall

- [ ] **Step 1: Add package to dnf install line**

In `src/backend/app/services/agent_deployer.py`, the `dnf install` command at line 65-66 currently reads:

```bash
    dnf install -y qemu-kvm libvirt libvirt-client virt-install \
        python3 python3-libvirt dnsmasq nftables xorriso nmap-ncat sshpass || true
```

Add `libguestfs-tools` to the package list:

```bash
    dnf install -y qemu-kvm libvirt libvirt-client virt-install \
        python3 python3-libvirt dnsmasq nftables xorriso nmap-ncat sshpass \
        libguestfs-tools || true
```

- [ ] **Step 2: Run black**

Run: `black src/backend/app/services/agent_deployer.py`

- [ ] **Step 3: Commit**

```bash
git add src/backend/app/services/agent_deployer.py
git commit -m "feat: add libguestfs-tools to agent installer for offline disk modification"
```

---

### Task 2: Add `/vms/modify-fs` Endpoint to Troshkad

**Files:**
- Modify: `src/troshkad/troshkad.py` (add handler function + register command)

**Interfaces:**
- Consumes: `COMMAND_HANDLERS` dict, `_job_log()`, `_run_cmd()` from troshkad internals
- Produces: `COMMAND_HANDLERS["vms/modify-fs"]` — accepts `{"disk": "/path/to.qcow2", "operations": [{"action": "rm-rf", "path": "/some/path"}, ...]}`, returns `{"results": [{"action": "...", "path": "...", "ok": true/false, "error": "..."}]}`

- [ ] **Step 1: Write the handler function**

Add this function to `src/troshkad/troshkad.py`, immediately after the `_handle_vm_set_clock` function and its `COMMAND_HANDLERS` registration (after line 1694):

```python
def _handle_vm_modify_fs(job, params):
    """Modify a guest filesystem offline using guestfish.

    Params:
        disk: path to qcow2 disk image (must not be in use by a running VM)
        operations: list of dicts, each with 'action' and action-specific fields
            - rm-rf: remove directory recursively (path)
            - rm-f: remove file, no error if missing (path)
            - mkdir-p: create directory with parents (path)
            - write: write content to file (path, content)
            - upload: upload local file to guest (local_path, path)
            - chmod: change permissions (mode, path)
    """
    disk = params.get("disk", "")
    operations = params.get("operations", [])
    if not disk or not operations:
        raise RuntimeError("disk and operations are required")
    if not os.path.exists(disk):
        raise RuntimeError(f"disk not found: {disk}")

    ALLOWED_ACTIONS = {"rm-rf", "rm-f", "mkdir-p", "write", "upload", "chmod"}
    guestfish_cmds = []
    for op in operations:
        action = op.get("action", "")
        if action not in ALLOWED_ACTIONS:
            raise RuntimeError(f"unsupported action: {action}")
        path = op.get("path", "")
        if not path:
            raise RuntimeError(f"path required for action: {action}")
        if action == "rm-rf":
            guestfish_cmds.append(f"rm-rf {path}")
        elif action == "rm-f":
            guestfish_cmds.append(f"rm-f {path}")
        elif action == "mkdir-p":
            guestfish_cmds.append(f"mkdir-p {path}")
        elif action == "write":
            content = op.get("content", "")
            guestfish_cmds.append(f"write {path} \"{content}\"")
        elif action == "upload":
            local_path = op.get("local_path", "")
            if not local_path:
                raise RuntimeError("local_path required for upload")
            guestfish_cmds.append(f"upload {local_path} {path}")
        elif action == "chmod":
            mode = op.get("mode", "")
            if not mode:
                raise RuntimeError("mode required for chmod")
            guestfish_cmds.append(f"chmod {mode} {path}")

    script = "\n".join(guestfish_cmds) + "\n"
    _job_log(job, f"Running guestfish on {disk} ({len(operations)} operations)")

    result = subprocess.run(
        ["guestfish", "--rw", "-a", disk, "-i"],
        input=script,
        capture_output=True,
        text=True,
        timeout=120,
    )

    results = []
    if result.returncode == 0:
        for op in operations:
            results.append({"action": op["action"], "path": op.get("path", ""), "ok": True})
        _job_log(job, f"All {len(operations)} operations succeeded")
    else:
        stderr = result.stderr.strip()
        _job_log(job, f"guestfish failed (rc={result.returncode}): {stderr}")
        for op in operations:
            results.append({
                "action": op["action"],
                "path": op.get("path", ""),
                "ok": False,
                "error": stderr,
            })

    return {"results": results}


COMMAND_HANDLERS["vms/modify-fs"] = _handle_vm_modify_fs
```

- [ ] **Step 2: Commit**

```bash
git add src/troshkad/troshkad.py
git commit -m "feat: add /vms/modify-fs endpoint for offline guest filesystem modification"
```

---

### Task 3: Add Cert Cleanup Step to Deploy Pipeline

**Files:**
- Modify: `src/backend/app/services/deploy_service.py` (add function + call it between Step 4 and Step 5)

**Interfaces:**
- Consumes: `_is_pattern_deploy()`, `_is_ocp_topology()`, `_extract_vms()`, `_find_vm_disks()`, `_disk_path()`, `start_job()`, `wait_for_job()` from existing deploy_service/troshkad_client
- Produces: `_clean_kubelet_certs(host, project_id, topology, pool)` — called in deploy pipeline, returns nothing (non-fatal, logs only)

- [ ] **Step 1: Write the cert cleanup function**

Add this function to `src/backend/app/services/deploy_service.py`, just before the `_is_ocp_topology` function (before line 2538):

```python
def _clean_kubelet_certs(host, project_id, topology, pool):
    """Delete stale kubelet PKI from RHCOS disks before VM startup.

    Uses guestfish via troshkad /vms/modify-fs to remove expired certs
    so kubelet bootstraps fresh ones on boot instead of retrying stale certs.
    Non-fatal — deploy continues regardless of outcome.
    """
    vms = _extract_vms(topology)
    rhcos_vms = [vm for vm in vms if vm.get("os") == "rhcos"]
    if not rhcos_vms:
        return

    operations = [
        {"action": "rm-rf", "path": "/var/lib/kubelet/pki"},
        {"action": "rm-f", "path": "/var/lib/kubelet/kubeconfig"},
    ]

    for vm in rhcos_vms:
        vm_disks = _find_vm_disks(vm["node_id"], topology)
        boot_disk = next(
            (d for d in vm_disks if d.get("format") == "qcow2"),
            None,
        )
        if not boot_disk:
            logger.warning(
                "Deploy %s: no qcow2 boot disk for RHCOS VM %s, skipping cert cleanup",
                project_id[:8],
                vm.get("name", vm["node_id"][:8]),
            )
            continue

        disk = _disk_path(
            project_id, vm["node_id"], boot_disk["node_id"], boot_disk["format"], pool
        )
        vm_name = vm.get("name", vm["node_id"][:8])
        logger.info(
            "Deploy %s: cleaning kubelet certs from %s", project_id[:8], vm_name
        )
        try:
            job_id = start_job(host, "/vms/modify-fs", {"disk": disk, "operations": operations})
            job = wait_for_job(host, job_id, timeout=120)
            if job.get("status") == "failed":
                logger.warning(
                    "Deploy %s: cert cleanup failed for %s: %s",
                    project_id[:8],
                    vm_name,
                    job.get("result", {}).get("error", "unknown"),
                )
            else:
                logger.info(
                    "Deploy %s: cert cleanup complete for %s", project_id[:8], vm_name
                )
        except Exception:
            logger.warning(
                "Deploy %s: cert cleanup error for %s, continuing",
                project_id[:8],
                vm_name,
                exc_info=True,
            )
```

- [ ] **Step 2: Call the function in the deploy pipeline**

In `src/backend/app/services/deploy_service.py`, find the block that checks `_project_deleted` right before Step 5 (around line 2355-2361). Insert the new step between that check and Step 5. The result should read:

```python
        if _project_deleted(project_id):
            logger.info(
                "Deploy %s: project deleted mid-deploy, aborting", project_id[:8]
            )
            _deploy_progress.pop(project_id, None)
            return

        # Step 4d: Clean kubelet certs on RHCOS disks (pattern + OCP only)
        if _is_pattern_deploy(topology) and _is_ocp_topology(topology):
            _update_deploy_progress(
                project_id, "certs", "cleaning kubelet certificates"
            )
            _clean_kubelet_certs(host, project_id, topology, pool)

        # Step 5: Start VMs (unless auto_start is disabled)
```

- [ ] **Step 3: Run black**

Run: `black src/backend/app/services/deploy_service.py`

- [ ] **Step 4: Commit**

```bash
git add src/backend/app/services/deploy_service.py
git commit -m "feat: clean kubelet certs before VM start on pattern OCP deploys"
```

---

### Task 4: Unit Tests

**Files:**
- Create: `src/backend/tests/test_cert_cleanup.py`

**Interfaces:**
- Consumes: `_clean_kubelet_certs`, `_is_pattern_deploy`, `_is_ocp_topology`, `_extract_vms`, `_find_vm_disks`, `_disk_path` from `deploy_service`

- [ ] **Step 1: Write the test file**

Create `src/backend/tests/test_cert_cleanup.py`:

```python
"""Tests for pre-boot kubelet cert cleanup during pattern deploys."""

from unittest.mock import MagicMock, patch

from app.services.deploy_service import (
    _clean_kubelet_certs,
    _disk_path,
    _extract_vms,
    _find_vm_disks,
    _is_ocp_topology,
    _is_pattern_deploy,
)


def _make_ocp_topology(vm_configs, with_pattern=True):
    """Build a minimal OCP topology with the given VM configs.

    vm_configs: list of dicts with keys: name, os, optional vcpus/ram
    Each VM gets one qcow2 storage node connected via a dp- edge.
    """
    nodes = []
    edges = []
    for i, cfg in enumerate(vm_configs):
        vm_id = f"vm-{i:04d}-0000-0000"
        disk_id = f"disk-{i:04d}-0000-0000"
        disk_ctrl_id = f"dp-{i}"
        nodes.append(
            {
                "id": vm_id,
                "type": "vmNode",
                "data": {
                    "name": cfg["name"],
                    "label": cfg.get("label", cfg["name"]),
                    "os": cfg["os"],
                    "vcpus": cfg.get("vcpus", 4),
                    "ram": cfg.get("ram", 16),
                    "diskControllers": [{"id": disk_ctrl_id, "bus": "virtio"}],
                },
            }
        )
        storage_data = {"size": 120, "format": "qcow2", "source": "blank"}
        if with_pattern:
            storage_data["patternId"] = "pat-0001"
            storage_data["patternDiskId"] = disk_id
        nodes.append(
            {
                "id": disk_id,
                "type": "storageNode",
                "data": storage_data,
            }
        )
        edges.append(
            {
                "source": vm_id,
                "target": disk_id,
                "sourceHandle": disk_ctrl_id,
                "targetHandle": "storage-in",
            }
        )
    return {"nodes": nodes, "edges": edges}


# -- Detection tests ----------------------------------------------------------


def test_is_ocp_topology_true():
    topo = _make_ocp_topology(
        [
            {"name": "bastion", "label": "bastion", "os": "rhel"},
            {"name": "cp-0", "os": "rhcos"},
        ]
    )
    assert _is_ocp_topology(topo) is True


def test_is_ocp_topology_false_no_rhcos():
    topo = _make_ocp_topology(
        [{"name": "bastion", "label": "bastion", "os": "rhel"}]
    )
    assert _is_ocp_topology(topo) is False


def test_is_ocp_topology_false_no_bastion():
    topo = _make_ocp_topology([{"name": "cp-0", "os": "rhcos"}])
    assert _is_ocp_topology(topo) is False


def test_is_pattern_deploy_true():
    topo = _make_ocp_topology(
        [{"name": "cp-0", "os": "rhcos"}], with_pattern=True
    )
    assert _is_pattern_deploy(topo) is True


def test_is_pattern_deploy_false():
    topo = _make_ocp_topology(
        [{"name": "cp-0", "os": "rhcos"}], with_pattern=False
    )
    assert _is_pattern_deploy(topo) is False


# -- RHCOS VM filtering -------------------------------------------------------


def test_extract_only_rhcos_vms():
    topo = _make_ocp_topology(
        [
            {"name": "bastion", "label": "bastion", "os": "rhel"},
            {"name": "cp-0", "os": "rhcos"},
            {"name": "cp-1", "os": "rhcos"},
        ]
    )
    vms = _extract_vms(topo)
    rhcos = [v for v in vms if v.get("os") == "rhcos"]
    assert len(rhcos) == 2
    assert all(v["os"] == "rhcos" for v in rhcos)


def test_find_boot_disk_for_vm():
    topo = _make_ocp_topology([{"name": "cp-0", "os": "rhcos"}])
    vms = _extract_vms(topo)
    disks = _find_vm_disks(vms[0]["node_id"], topo)
    boot = next((d for d in disks if d.get("format") == "qcow2"), None)
    assert boot is not None
    assert boot["format"] == "qcow2"


# -- Cert cleanup integration (mocked troshkad) -------------------------------


@patch("app.services.deploy_service.wait_for_job")
@patch("app.services.deploy_service.start_job")
def test_clean_kubelet_certs_calls_modify_fs(mock_start, mock_wait):
    """Verify cert cleanup calls /vms/modify-fs for each RHCOS VM."""
    mock_start.return_value = "job-001"
    mock_wait.return_value = {"status": "complete", "result": {"results": []}}
    host = MagicMock()

    topo = _make_ocp_topology(
        [
            {"name": "bastion", "label": "bastion", "os": "rhel"},
            {"name": "cp-0", "os": "rhcos"},
            {"name": "cp-1", "os": "rhcos"},
            {"name": "worker-0", "os": "rhcos"},
        ]
    )

    _clean_kubelet_certs(host, "proj-0001-0000", topo, pool=None)

    # Should be called 3 times (cp-0, cp-1, worker-0) — NOT for bastion
    assert mock_start.call_count == 3
    for call in mock_start.call_args_list:
        args = call[0]
        assert args[1] == "/vms/modify-fs"
        params = args[2]
        assert params["operations"] == [
            {"action": "rm-rf", "path": "/var/lib/kubelet/pki"},
            {"action": "rm-f", "path": "/var/lib/kubelet/kubeconfig"},
        ]
        assert params["disk"].endswith(".qcow2")


@patch("app.services.deploy_service.wait_for_job")
@patch("app.services.deploy_service.start_job")
def test_clean_kubelet_certs_nonfatal_on_failure(mock_start, mock_wait):
    """Verify cert cleanup does not raise on failure."""
    mock_start.return_value = "job-001"
    mock_wait.return_value = {
        "status": "failed",
        "result": {"error": "guestfish crashed"},
    }
    host = MagicMock()

    topo = _make_ocp_topology(
        [
            {"name": "bastion", "label": "bastion", "os": "rhel"},
            {"name": "cp-0", "os": "rhcos"},
        ]
    )

    # Should not raise
    _clean_kubelet_certs(host, "proj-0001-0000", topo, pool=None)
    assert mock_start.call_count == 1


@patch("app.services.deploy_service.wait_for_job")
@patch("app.services.deploy_service.start_job")
def test_clean_kubelet_certs_nonfatal_on_exception(mock_start, mock_wait):
    """Verify cert cleanup does not raise on troshkad exception."""
    mock_start.side_effect = Exception("connection refused")
    host = MagicMock()

    topo = _make_ocp_topology(
        [
            {"name": "bastion", "label": "bastion", "os": "rhel"},
            {"name": "cp-0", "os": "rhcos"},
        ]
    )

    # Should not raise
    _clean_kubelet_certs(host, "proj-0001-0000", topo, pool=None)


@patch("app.services.deploy_service.wait_for_job")
@patch("app.services.deploy_service.start_job")
def test_clean_kubelet_certs_sno(mock_start, mock_wait):
    """Verify cert cleanup works for SNO (single RHCOS node)."""
    mock_start.return_value = "job-001"
    mock_wait.return_value = {"status": "complete", "result": {"results": []}}
    host = MagicMock()

    topo = _make_ocp_topology(
        [
            {"name": "bastion", "label": "bastion", "os": "rhel"},
            {"name": "cp-0", "os": "rhcos"},
        ]
    )

    _clean_kubelet_certs(host, "proj-0001-0000", topo, pool=None)

    # SNO: exactly 1 RHCOS VM
    assert mock_start.call_count == 1


@patch("app.services.deploy_service.wait_for_job")
@patch("app.services.deploy_service.start_job")
def test_clean_kubelet_certs_skips_non_qcow2(mock_start, mock_wait):
    """Verify cert cleanup skips VMs whose only disk is not qcow2."""
    host = MagicMock()

    topo = _make_ocp_topology(
        [
            {"name": "bastion", "label": "bastion", "os": "rhel"},
            {"name": "cp-0", "os": "rhcos"},
        ]
    )
    # Change the storage node format to iso
    for n in topo["nodes"]:
        if n["type"] == "storageNode" and n["id"] == "disk-0001-0000-0000":
            n["data"]["format"] = "iso"

    _clean_kubelet_certs(host, "proj-0001-0000", topo, pool=None)

    # Should skip cp-0 because its disk is iso, not qcow2
    assert mock_start.call_count == 0
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_cert_cleanup.py -v`

Expected: All tests pass. (Note: tests for detection functions will pass immediately since they test existing code. Tests for `_clean_kubelet_certs` will fail until Task 3 is completed — if executing tasks in order, Task 3 should be done first. If using subagent-driven development, Tasks 3 and 4 can run in either order since the test file mocks all external calls.)

- [ ] **Step 3: Run black**

Run: `black src/backend/tests/test_cert_cleanup.py`

- [ ] **Step 4: Commit**

```bash
git add src/backend/tests/test_cert_cleanup.py
git commit -m "test: add unit tests for kubelet cert cleanup during pattern deploys"
```
