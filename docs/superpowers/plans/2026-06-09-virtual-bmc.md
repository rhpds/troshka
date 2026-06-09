# Virtual BMC (IPMI & Redfish) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add virtual BMC endpoints (Redfish via sushy-tools, IPMI via virtualbmc) for VMs so users can run OpenShift bare-metal installations (IPI, UPI, Agent-Based) inside Troshka projects.

**Architecture:** Per-VM BMC endpoints run inside the project's network namespace on a dedicated BMC bridge. Troshkad manages sushy-emulator and vbmcd processes as subprocesses (same pattern as dnsmasq). The frontend auto-creates a BMC network node when any VM enables BMC, and displays copyable Redfish/IPMI addresses after deploy.

**Tech Stack:** Python (sushy-tools, virtualbmc, libvirt-python in /opt/troshka/venv), FastAPI backend, Next.js + PatternFly frontend, Zustand canvas store.

**Spec:** `docs/superpowers/specs/2026-06-09-virtual-bmc-design.md`

---

## Task 1: Agent Installer — Install BMC Dependencies

**Files:**
- Modify: `src/backend/app/services/agent_deployer.py:32-33` (DNF list), add venv creation after troshkad install

This task adds the Python venv with sushy-tools and virtualbmc to the agent install script that runs on each host.

- [ ] **Step 1: Add system packages to DNF install list**

In `src/backend/app/services/agent_deployer.py`, find the DNF install line (line 32-33):

```python
dnf install -y qemu-kvm libvirt libvirt-client virt-install \
    python3 python3-libvirt dnsmasq nftables xorriso nmap-ncat || true
```

Add `python3-devel pkg-config gcc` to support compiling libvirt-python:

```python
dnf install -y qemu-kvm libvirt libvirt-client virt-install \
    python3 python3-libvirt python3-devel pkg-config gcc dnsmasq nftables xorriso nmap-ncat || true
```

- [ ] **Step 2: Add venv creation to install script**

In the same `AGENT_INSTALL_SCRIPT` heredoc, after the troshkad systemd unit setup (around line 206) and before the final credential output, add:

```bash
# BMC tools venv (sushy-tools for Redfish, virtualbmc for IPMI)
echo "=== Setting up BMC tools venv ==="
python3 -m venv /opt/troshka/venv
/opt/troshka/venv/bin/pip install --quiet sushy-tools virtualbmc libvirt-python
echo "BMC venv ready at /opt/troshka/venv"
```

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/agent_deployer.py
git commit -m "feat: install sushy-tools and virtualbmc in agent venv for BMC support"
```

---

## Task 2: Troshkad — BMC Setup Handler

**Files:**
- Modify: `src/troshkad/troshkad.py` — add `_handle_bmc_setup()`, `_handle_bmc_teardown()`, `_handle_bmc_status()` handlers

This is the largest task — it adds three new command handlers to troshkad for managing BMC processes. All processes run inside the project's network namespace.

- [ ] **Step 1: Add the BMC setup handler**

At the end of `src/troshkad/troshkad.py` (before the final `if __name__` block), add the BMC setup handler. This creates the BMC bridge, starts one sushy-emulator per VM, and one vbmcd managing all IPMI entries:

```python
def _handle_bmc_setup(job, params):
    """Set up virtual BMC endpoints for a project's VMs.

    Creates a BMC bridge inside the project namespace, starts sushy-emulator
    (Redfish) and vbmcd/vbmc (IPMI) for each BMC-enabled VM.

    Params:
        project_id: str
        bmc_cidr: str — e.g. "192.168.100.0/24"
        bmc_gateway_ip: str — e.g. "192.168.100.1"
        bmc_username: str
        bmc_password: str
        vms: list of {domain_name: str, bmc_ip: str}
    """
    project_id = _validate_project_id(params["project_id"])
    bmc_cidr = params["bmc_cidr"]
    bmc_gateway_ip = params["bmc_gateway_ip"]
    bmc_username = params.get("bmc_username", "admin")
    bmc_password = params.get("bmc_password", "password")
    vms = params.get("vms", [])

    if not vms:
        job["output"].append("No BMC-enabled VMs, skipping")
        return {"status": "skipped"}

    pid = project_id[:8]
    ns = f"troshka-{pid}"
    bridge = f"br-bmc-{pid}"
    prefix = bmc_cidr.split("/")[1] if "/" in bmc_cidr else "24"
    bmc_dir = f"/var/lib/troshka/bmc/{project_id}"
    venv_bin = "/opt/troshka/venv/bin"

    os.makedirs(bmc_dir, exist_ok=True)

    # 1. Create BMC bridge inside namespace
    try:
        _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "del", bridge], timeout=10)
    except RuntimeError:
        pass
    _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "add", bridge, "type", "bridge"], timeout=10)
    _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "addr", "add",
                    f"{bmc_gateway_ip}/{prefix}", "dev", bridge], timeout=10)
    _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "set", bridge, "up"], timeout=10)

    # Also create dummy bridge in host namespace for libvirt validation
    try:
        subprocess.run(["ip", "link", "show", bridge], capture_output=True, check=True, timeout=5)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        _run_cmd(job, ["ip", "link", "add", bridge, "type", "bridge"], timeout=10)
    _run_cmd(job, ["ip", "link", "set", bridge, "up"], timeout=10)

    job["output"].append(f"BMC bridge {bridge} created in namespace {ns}")

    # 2. Assign BMC IPs to the bridge (one per VM) so sushy/vbmc can bind
    for vm in vms:
        bmc_ip = _validate_ip(vm["bmc_ip"])
        _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "addr", "add",
                        f"{bmc_ip}/{prefix}", "dev", bridge], timeout=10)

    # 3. Create htpasswd file for sushy basic auth
    htpasswd_path = os.path.join(bmc_dir, "htpasswd")
    import hashlib
    # Apache htpasswd format: user:{SHA}base64hash
    import base64
    sha_hash = base64.b64encode(hashlib.sha1(bmc_password.encode()).digest()).decode()
    with open(htpasswd_path, "w") as f:
        f.write(f"{bmc_username}:{{SHA}}{sha_hash}\n")

    # 4. Start sushy-emulator per VM
    for vm in vms:
        domain_name = _validate_domain_name(vm["domain_name"])
        bmc_ip = _validate_ip(vm["bmc_ip"])
        vm_short = domain_name.split("-")[-1] if "-" in domain_name else domain_name[:8]

        # Write sushy config
        conf_path = os.path.join(bmc_dir, f"sushy-{vm_short}.conf")
        with open(conf_path, "w") as f:
            f.write(f"SUSHY_EMULATOR_LISTEN_IP = '{bmc_ip}'\n")
            f.write("SUSHY_EMULATOR_LISTEN_PORT = 8000\n")
            f.write("SUSHY_EMULATOR_LIBVIRT_URI = 'qemu:///system'\n")
            f.write("SUSHY_EMULATOR_FEATURE_SET = 'vmedia'\n")
            f.write("SUSHY_EMULATOR_IGNORE_BOOT_DEVICE = False\n")
            f.write(f"SUSHY_EMULATOR_AUTH_FILE = '{htpasswd_path}'\n")
            f.write(f"SUSHY_EMULATOR_ALLOWED_INSTANCES = ['{domain_name}']\n")

        pid_path = os.path.join(bmc_dir, f"sushy-{vm_short}.pid")

        # Kill existing if any
        if os.path.exists(pid_path):
            try:
                with open(pid_path) as f:
                    old_pid = int(f.read().strip())
                os.kill(old_pid, signal.SIGTERM)
            except (ValueError, ProcessLookupError, PermissionError):
                pass

        # Start sushy-emulator in namespace
        proc = subprocess.Popen(
            ["ip", "netns", "exec", ns, f"{venv_bin}/sushy-emulator", "--config", conf_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        with open(pid_path, "w") as f:
            f.write(str(proc.pid))

        job["output"].append(f"sushy-emulator started for {domain_name} at {bmc_ip}:8000 (PID {proc.pid})")

    # 5. Start vbmcd and register VMs for IPMI
    vbmcd_conf_dir = os.path.join(bmc_dir, "vbmcd")
    os.makedirs(vbmcd_conf_dir, exist_ok=True)

    # Write vbmcd config
    vbmcd_conf_path = os.path.join(bmc_dir, "virtualbmc.conf")
    with open(vbmcd_conf_path, "w") as f:
        f.write("[default]\n")
        f.write(f"config_dir = {vbmcd_conf_dir}\n")
        f.write(f"pid_file = {bmc_dir}/vbmcd.pid\n")
        f.write("[log]\n")
        f.write(f"logfile = {bmc_dir}/vbmcd.log\n")

    # Kill existing vbmcd
    vbmcd_pid_path = os.path.join(bmc_dir, "vbmcd.pid")
    if os.path.exists(vbmcd_pid_path):
        try:
            with open(vbmcd_pid_path) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, signal.SIGTERM)
            import time
            time.sleep(1)
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    # Start vbmcd in namespace
    env = os.environ.copy()
    env["VIRTUALBMC_CONFIG"] = vbmcd_conf_path
    proc = subprocess.Popen(
        ["ip", "netns", "exec", ns, f"{venv_bin}/vbmcd", "--foreground"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env, start_new_session=True,
    )
    with open(vbmcd_pid_path, "w") as f:
        f.write(str(proc.pid))
    import time
    time.sleep(2)  # Wait for vbmcd to start

    job["output"].append(f"vbmcd started (PID {proc.pid})")

    # Register each VM with vbmc
    for vm in vms:
        domain_name = _validate_domain_name(vm["domain_name"])
        bmc_ip = _validate_ip(vm["bmc_ip"])

        # vbmc add + start (must also run in namespace for ZMQ to reach vbmcd)
        _run_cmd(job, ["ip", "netns", "exec", ns, f"{venv_bin}/vbmc", "add", domain_name,
                        "--port", "623", "--address", bmc_ip,
                        "--username", bmc_username, "--password", bmc_password,
                        "--libvirt-uri", "qemu:///system"], timeout=30)
        _run_cmd(job, ["ip", "netns", "exec", ns, f"{venv_bin}/vbmc", "start", domain_name], timeout=30)
        job["output"].append(f"vbmc registered {domain_name} at {bmc_ip}:623")

    return {
        "status": "ok",
        "bmc_bridge": bridge,
        "vm_count": len(vms),
    }

COMMAND_HANDLERS["bmc/setup"] = _handle_bmc_setup
```

- [ ] **Step 2: Add the BMC teardown handler**

```python
def _handle_bmc_teardown(job, params):
    """Tear down all BMC endpoints for a project.

    Stops sushy-emulators, vbmcd, removes BMC bridge and config.

    Params:
        project_id: str
    """
    project_id = _validate_project_id(params["project_id"])
    pid = project_id[:8]
    ns = f"troshka-{pid}"
    bridge = f"br-bmc-{pid}"
    bmc_dir = f"/var/lib/troshka/bmc/{project_id}"
    venv_bin = "/opt/troshka/venv/bin"

    killed = 0

    # 1. Kill sushy-emulator processes (by PID files)
    if os.path.isdir(bmc_dir):
        for fname in os.listdir(bmc_dir):
            if fname.startswith("sushy-") and fname.endswith(".pid"):
                pid_path = os.path.join(bmc_dir, fname)
                try:
                    with open(pid_path) as f:
                        p = int(f.read().strip())
                    os.kill(p, signal.SIGTERM)
                    killed += 1
                    job["output"].append(f"Killed sushy-emulator PID {p}")
                except (ValueError, ProcessLookupError, PermissionError):
                    pass

    # 2. Stop all vbmc entries and kill vbmcd
    vbmcd_pid_path = os.path.join(bmc_dir, "vbmcd.pid")
    vbmcd_conf_path = os.path.join(bmc_dir, "virtualbmc.conf")
    if os.path.exists(vbmcd_pid_path):
        # Try to stop all vbmc entries first
        env = os.environ.copy()
        if os.path.exists(vbmcd_conf_path):
            env["VIRTUALBMC_CONFIG"] = vbmcd_conf_path
        try:
            result = subprocess.run(
                ["ip", "netns", "exec", ns, f"{venv_bin}/vbmc", "list"],
                capture_output=True, text=True, env=env, timeout=10,
            )
            for line in result.stdout.splitlines():
                parts = line.split()
                if parts and parts[0].startswith("troshka-"):
                    domain = parts[0]
                    try:
                        _run_cmd(job, ["ip", "netns", "exec", ns, f"{venv_bin}/vbmc", "stop", domain], timeout=10)
                        _run_cmd(job, ["ip", "netns", "exec", ns, f"{venv_bin}/vbmc", "delete", domain], timeout=10)
                    except RuntimeError:
                        pass
        except (subprocess.TimeoutExpired, RuntimeError):
            pass

        # Kill vbmcd
        try:
            with open(vbmcd_pid_path) as f:
                p = int(f.read().strip())
            os.kill(p, signal.SIGTERM)
            killed += 1
            job["output"].append(f"Killed vbmcd PID {p}")
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    # 3. Remove BMC bridge from namespace
    try:
        _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "del", bridge], timeout=10)
        job["output"].append(f"Removed BMC bridge {bridge} from namespace")
    except RuntimeError:
        pass

    # Remove dummy bridge from host
    try:
        _run_cmd(job, ["ip", "link", "del", bridge], timeout=10)
    except RuntimeError:
        pass

    # 4. Remove BMC config directory
    if os.path.isdir(bmc_dir):
        shutil.rmtree(bmc_dir, ignore_errors=True)
        job["output"].append(f"Removed BMC config dir: {bmc_dir}")

    return {"status": "ok", "killed": killed}

COMMAND_HANDLERS["bmc/teardown"] = _handle_bmc_teardown
```

- [ ] **Step 3: Add the BMC status handler**

```python
def _handle_bmc_status(job, params):
    """Check status of BMC processes for a project.

    Params:
        project_id: str
    """
    project_id = _validate_project_id(params["project_id"])
    bmc_dir = f"/var/lib/troshka/bmc/{project_id}"

    result = {"sushy_processes": [], "vbmcd_running": False}

    if not os.path.isdir(bmc_dir):
        return result

    # Check sushy-emulator PIDs
    for fname in os.listdir(bmc_dir):
        if fname.startswith("sushy-") and fname.endswith(".pid"):
            pid_path = os.path.join(bmc_dir, fname)
            try:
                with open(pid_path) as f:
                    p = int(f.read().strip())
                os.kill(p, 0)  # Check if alive
                result["sushy_processes"].append({"pid": p, "file": fname, "alive": True})
            except (ValueError, ProcessLookupError, PermissionError, FileNotFoundError):
                result["sushy_processes"].append({"file": fname, "alive": False})

    # Check vbmcd PID
    vbmcd_pid_path = os.path.join(bmc_dir, "vbmcd.pid")
    if os.path.exists(vbmcd_pid_path):
        try:
            with open(vbmcd_pid_path) as f:
                p = int(f.read().strip())
            os.kill(p, 0)
            result["vbmcd_running"] = True
        except (ValueError, ProcessLookupError, PermissionError):
            pass

    return result

COMMAND_HANDLERS["bmc/status"] = _handle_bmc_status
```

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/troshkad/troshkad.py
git commit -m "feat: add bmc/setup, bmc/teardown, bmc/status handlers to troshkad"
```

---

## Task 3: Backend — Deploy Service BMC Integration

**Files:**
- Modify: `src/backend/app/services/deploy_service.py` — add BMC steps to deploy/undeploy/stop/start flows

- [ ] **Step 1: Add BMC helper functions**

Add these functions near the top of `deploy_service.py` (after the existing helper functions around line 150):

```python
def _extract_bmc_config(topology: dict, project_id: str) -> dict | None:
    """Extract BMC configuration from topology if any VMs have BMC enabled.

    Returns None if no VMs have BMC enabled, or a dict with:
        bmc_network: the BMC network node data
        vms: list of {node_id, domain_name, bmc_ip}
    """
    bmc_network = None
    for node in topology.get("nodes", []):
        if node.get("type") == "networkNode" and node.get("data", {}).get("networkType") == "bmc":
            bmc_network = node
            break

    if not bmc_network:
        return None

    bmc_vms = []
    for node in topology.get("nodes", []):
        if node.get("type") == "vmNode" and node.get("data", {}).get("bmcEnabled"):
            bmc_ip = node["data"].get("bmcIp", "")
            if bmc_ip:
                bmc_vms.append({
                    "node_id": node["id"],
                    "domain_name": _vm_domain_name(project_id, node["id"]),
                    "bmc_ip": bmc_ip,
                })

    if not bmc_vms:
        return None

    return {
        "bmc_network": bmc_network["data"],
        "vms": bmc_vms,
    }


def _setup_bmc_via_troshkad(host, project_id: str, bmc_config: dict):
    """Start BMC endpoints (Redfish + IPMI) on the host for this project."""
    from app.services.troshkad_client import start_job, wait_for_job

    net_data = bmc_config["bmc_network"]
    params = {
        "project_id": project_id,
        "bmc_cidr": net_data.get("cidr", "192.168.100.0/24"),
        "bmc_gateway_ip": net_data.get("cidr", "192.168.100.0/24").rsplit(".", 1)[0] + ".1",
        "bmc_username": net_data.get("bmcUsername", "admin"),
        "bmc_password": net_data.get("bmcPassword", "password"),
        "vms": [{"domain_name": vm["domain_name"], "bmc_ip": vm["bmc_ip"]} for vm in bmc_config["vms"]],
    }
    job_id = start_job(host, "/bmc/setup", params)
    job = wait_for_job(host, job_id, timeout=120)
    if job["status"] == "failed":
        error = job.get("result", {}).get("error", "BMC setup failed")
        return error
    return True


def _teardown_bmc_via_troshkad(host, project_id: str):
    """Stop all BMC endpoints and remove BMC bridge for this project."""
    from app.services.troshkad_client import start_job, wait_for_job

    job_id = start_job(host, "/bmc/teardown", {"project_id": project_id})
    job = wait_for_job(host, job_id, timeout=60)
    if job["status"] == "failed":
        logger.warning("BMC teardown failed for %s: %s", project_id[:8], job.get("result"))
```

- [ ] **Step 2: Add BMC step to `deploy_project_async()`**

In `deploy_project_async()`, add two new steps. After the VM creation loop (line 760, `_create_vm_via_troshkad`) and before VM startup (line 763, `_start_vms_via_troshkad`):

```python
        # Step 4b: Start BMC endpoints (after VMs are defined, before startup)
        bmc_config = _extract_bmc_config(topology, project_id)
        if bmc_config:
            _deploy_progress[project_id] = {"step": "bmc", "detail": "starting BMC endpoints"}
            notify_project(project_id, {"type": "deploy-progress", "progress": _deploy_progress[project_id]})
            logger.info("Deploy %s: starting BMC endpoints for %d VMs", project_id[:8], len(bmc_config["vms"]))
            bmc_result = _setup_bmc_via_troshkad(host, project_id, bmc_config)
            if bmc_result is not True:
                logger.error("Deploy %s: BMC setup failed: %s", project_id[:8], bmc_result)
                project.state = "error"
                project.deploy_error = f"BMC setup failed: {bmc_result}"
                s.commit()
                _deploy_progress.pop(project_id, None)
                return
```

Also, after the deploy succeeds (around line 770, after `project.state = "active"`), store BMC addresses in deployed_topology:

```python
        # Store BMC addresses in deployed topology for UI display
        if bmc_config:
            import secrets
            deployed_topo = project.deployed_topology or {}
            deployed_topo["bmc"] = {
                "username": bmc_config["bmc_network"].get("bmcUsername", "admin"),
                "password": bmc_config["bmc_network"].get("bmcPassword", "password"),
                "vms": {
                    vm["node_id"]: {
                        "ip": vm["bmc_ip"],
                        "redfish_url": f"redfish-virtualmedia://{vm['bmc_ip']}:8000/redfish/v1/Systems/{vm['domain_name']}",
                        "ipmi_address": f"{vm['bmc_ip']}:623",
                    }
                    for vm in bmc_config["vms"]
                },
            }
            project.deployed_topology = deployed_topo
            s.commit()
```

- [ ] **Step 3: Add BMC teardown to `stop_project_async()`**

In `stop_project_async()`, add BMC teardown after stopping VMs but before network teardown. Find the network teardown section (around line 824) and add before it:

```python
        # Tear down BMC endpoints before network teardown
        bmc_config = _extract_bmc_config(topology, project_id)
        if bmc_config:
            logger.info("Stop %s: tearing down BMC endpoints", project_id[:8])
            _teardown_bmc_via_troshkad(host, project_id)
```

- [ ] **Step 4: Add BMC setup to `start_project_async()`**

In `start_project_async()`, add BMC setup after network setup and VM startup. Find where VMs are started and add after:

```python
        # Re-start BMC endpoints
        bmc_config = _extract_bmc_config(topology, project_id)
        if bmc_config:
            logger.info("Start %s: re-starting BMC endpoints", project_id[:8])
            _setup_bmc_via_troshkad(host, project_id, bmc_config)
```

- [ ] **Step 5: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/deploy_service.py
git commit -m "feat: integrate BMC setup/teardown into deploy/stop/start flows"
```

---

## Task 4: Backend — Deploy Validation

**Files:**
- Modify: `src/backend/app/api/projects.py:126-172` — add BMC network validation before deploy

- [ ] **Step 1: Add BMC validation to `deploy_project()` endpoint**

In `src/backend/app/api/projects.py`, after the existing validation checks (around line 143, after the VM count check), add:

```python
    # Validate BMC network has at least one connected provisioner VM
    topology = project.topology or {}
    bmc_network = None
    for node in topology.get("nodes", []):
        if node.get("type") == "networkNode" and node.get("data", {}).get("networkType") == "bmc":
            bmc_network = node
            break
    if bmc_network:
        bmc_edges = [
            e for e in topology.get("edges", [])
            if e.get("source") == bmc_network["id"] or e.get("target") == bmc_network["id"]
        ]
        if not bmc_edges:
            raise HTTPException(
                status_code=400,
                detail="BMC network requires at least one connected VM to act as a provisioner",
            )
```

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/projects.py
git commit -m "feat: block deploy if BMC network has no connected provisioner VM"
```

---

## Task 5: Backend — Topology Remapping for Patterns

**Files:**
- Modify: `src/backend/app/api/patterns.py:46-136` — add BMC field remapping

- [ ] **Step 1: Add BMC remapping to `_remap_topology()`**

In `_remap_topology()` in `src/backend/app/api/patterns.py`, after the existing node-level remapping (after line 79, the disk controller loop), add BMC IP reassignment. And after edge remapping, handle the BMC network node:

After the disk controller loop (line 79):

```python
        # bmcIp: keep the value (it's relative to the BMC CIDR, not project-specific)
        # bmcEnabled: preserved as-is
```

No actual code change needed for per-VM fields since `bmcEnabled` and `bmcIp` are just data fields that survive the deepcopy and node ID remap.

The BMC network node's `bmcPassword` is preserved automatically by deepcopy — this is intentional (pattern lab instructions stay stable, per spec).

The BMC network node ID gets remapped via the existing `id_map` loop (line 60-64), so edges to/from it are correctly updated.

- [ ] **Step 2: Verify edge remapping handles BMC network**

The existing edge remapping (lines 97-111) already handles edges to any node type — the BMC network node is just another `networkNode` with a different `data.networkType`. No changes needed.

- [ ] **Step 3: Commit**

No code changes required — the existing remapping logic handles BMC data correctly. Add a comment for clarity:

After line 53 in `_remap_topology()`, add to the docstring:

```python
    """Clone a topology dict with all-new UUIDs, MACs, and controller IDs.

    - Every node gets a new UUID-based ``id``
    - Edges are updated to reference the new node IDs and handle IDs
    - NIC MAC addresses are regenerated
    - NIC ids and diskController ids are regenerated
    - Network CIDRs, DHCP ranges, DNS domains are preserved
    - BMC network credentials (bmcPassword) are preserved for pattern stability
    """
```

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/patterns.py
git commit -m "docs: document BMC credential preservation in topology remapping"
```

---

## Task 6: Backend — GC Integration

**Files:**
- Modify: `src/backend/app/services/gc_service.py:41-103` — add BMC orphan discovery and cleanup

- [ ] **Step 1: Add BMC orphan fields to `discover_orphans()`**

In `discover_orphans()`, after building `known_domains` (line 59), add BMC tracking:

```python
    # Build list of project IDs that should have BMC
    bmc_project_ids = set()
    for p in db.query(Project).filter(Project.host_id == host.id).all():
        if p.state in ("active", "stopped"):
            topo = p.deployed_topology or p.topology or {}
            for node in topo.get("nodes", []):
                if node.get("type") == "networkNode" and node.get("data", {}).get("networkType") == "bmc":
                    bmc_project_ids.add(p.id)
                    break
```

Then after the troshkad discover call (line 66), add BMC orphan detection:

```python
    # Discover orphaned BMC dirs
    result = job["result"]
    result["orphaned_bmc_dirs"] = []
    bmc_base = "/var/lib/troshka/bmc"
    # We'll ask troshkad to check — add to the discover params
```

Actually, simpler approach — add BMC cleanup to the `clean_orphans()` call. In `clean_orphans()` (line 73), add `orphan_bmc_project_ids` to the params:

```python
    job_id = start_job(host, "/gc/clean", {
        "orphan_dirs": [op.get("project_id") if isinstance(op, dict) else op for op in orphans.get("orphaned_projects", [])],
        "orphan_domains": orphans.get("orphaned_domains", []),
        "orphan_bridges": orphans.get("orphaned_bridges", []),
        "orphan_namespaces": orphans.get("orphaned_namespaces", []),
        "cache_items": cache_items,
        "orphan_bmc_project_ids": orphans.get("orphaned_bmc_project_ids", []),
    })
```

- [ ] **Step 2: Add BMC cleanup to troshkad's `_handle_gc_clean()`**

In `src/troshkad/troshkad.py`, in `_handle_gc_clean()` (line 2165), add after the cache cleanup section (before the return):

```python
    # 6. Clean up orphaned BMC resources
    orphan_bmc_ids = params.get("orphan_bmc_project_ids", [])
    removed_bmc = 0
    for project_id in orphan_bmc_ids:
        bmc_dir = f"/var/lib/troshka/bmc/{project_id}"
        # Kill any BMC processes by PID files
        if os.path.isdir(bmc_dir):
            for fname in os.listdir(bmc_dir):
                if fname.endswith(".pid"):
                    pid_path = os.path.join(bmc_dir, fname)
                    try:
                        with open(pid_path) as f:
                            p = int(f.read().strip())
                        os.kill(p, signal.SIGTERM)
                        job["output"].append(f"Killed BMC process PID {p} ({fname})")
                    except (ValueError, ProcessLookupError, PermissionError, FileNotFoundError):
                        pass
            shutil.rmtree(bmc_dir, ignore_errors=True)
            job["output"].append(f"Removed BMC dir: {bmc_dir}")
            removed_bmc += 1

        # Remove BMC bridge
        pid_short = project_id[:8]
        bridge = f"br-bmc-{pid_short}"
        ns = f"troshka-{pid_short}"
        try:
            _run_cmd(job, ["ip", "netns", "exec", ns, "ip", "link", "del", bridge], timeout=10)
        except RuntimeError:
            pass
        try:
            _run_cmd(job, ["ip", "link", "del", bridge], timeout=10)
        except RuntimeError:
            pass
```

Update the return dict to include `removed_bmc`.

- [ ] **Step 3: Add BMC orphan detection to `discover_orphans()` params**

In `gc_service.py`, update the `discover` call to include known BMC project IDs, so troshkad can report orphans:

```python
    job_id = start_job(host, "/gc/discover", {
        "known_project_ids": active_project_ids,
        "known_domains": known_domains,
        "known_bmc_project_ids": list(bmc_project_ids),
    })
```

And in troshkad's `_handle_gc_discover()`, add BMC orphan detection — scan `/var/lib/troshka/bmc/` for project dirs not in `known_bmc_project_ids`:

```python
    # Discover orphaned BMC directories
    orphaned_bmc = []
    bmc_base = "/var/lib/troshka/bmc"
    known_bmc = set(params.get("known_bmc_project_ids", []))
    if os.path.isdir(bmc_base):
        for entry in os.listdir(bmc_base):
            full = os.path.join(bmc_base, entry)
            if os.path.isdir(full) and entry not in known_bmc:
                orphaned_bmc.append(entry)
                job["output"].append(f"Orphaned BMC dir: {entry}")
    result["orphaned_bmc_project_ids"] = orphaned_bmc
```

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/services/gc_service.py src/troshkad/troshkad.py
git commit -m "feat: add BMC orphan detection and cleanup to garbage collector"
```

---

## Task 7: Frontend — Canvas Store BMC Logic

**Files:**
- Modify: `src/frontend/src/stores/canvasStore.ts` — add BMC network auto-create/remove, NetworkNodeData type update

- [ ] **Step 1: Update NetworkNodeData type**

In `src/frontend/src/stores/canvasStore.ts`, update the `NetworkNodeData` interface (line 48) to include BMC fields. The `[key: string]: any` index signature already covers arbitrary fields, so this is optional but improves code clarity. No type change strictly needed — the index signature handles it.

- [ ] **Step 2: Add BMC network auto-create/remove helper**

Add a helper function that the properties panel can call when BMC is toggled. Add this after the store definition (before the auto-save subscription at line 904):

```typescript
/**
 * Auto-create or remove the BMC network node based on whether any VMs have BMC enabled.
 * Called by PropertiesPanel when bmcEnabled is toggled.
 */
export function syncBmcNetwork() {
  const state = useCanvasStore.getState();
  const nodes = state.nodes;

  const hasBmcVm = nodes.some(
    (n) => n.type === "vmNode" && (n.data as Record<string, any>).bmcEnabled
  );
  const bmcNetNode = nodes.find(
    (n) => n.type === "networkNode" && (n.data as Record<string, any>).networkType === "bmc"
  );

  if (hasBmcVm && !bmcNetNode) {
    // Auto-create BMC network node
    const chars = "abcdefghijklmnopqrstuvwxyz0123456789";
    const password = Array.from({ length: 16 }, () => chars[Math.floor(Math.random() * chars.length)]).join("");

    // Position near the center of existing VM nodes
    const vmNodes = nodes.filter((n) => n.type === "vmNode" && (n.data as Record<string, any>).bmcEnabled);
    const avgX = vmNodes.reduce((sum, n) => sum + (n.position?.x || 0), 0) / Math.max(vmNodes.length, 1);
    const avgY = vmNodes.reduce((sum, n) => sum + (n.position?.y || 0), 0) / Math.max(vmNodes.length, 1);

    const bmcNode = {
      id: `bmc-network-${Date.now()}`,
      type: "networkNode",
      position: { x: avgX + 300, y: avgY },
      data: {
        label: "BMC Network",
        name: "BMC Network",
        subtype: "network" as const,
        networkType: "bmc",
        cidr: "192.168.100.0/24",
        dhcp: true,
        dns: false,
        bmcUsername: "admin",
        bmcPassword: password,
      },
    };
    state.addNode(bmcNode);
  } else if (!hasBmcVm && bmcNetNode) {
    // Auto-remove BMC network and its edges
    state.deleteNode(bmcNetNode.id);
  }
}

/**
 * Allocate the next available BMC IP from the BMC network CIDR.
 */
export function allocateBmcIp(): string {
  const state = useCanvasStore.getState();
  const nodes = state.nodes;

  const bmcNet = nodes.find(
    (n) => n.type === "networkNode" && (n.data as Record<string, any>).networkType === "bmc"
  );
  const cidr = (bmcNet?.data as Record<string, any>)?.cidr || "192.168.100.0/24";
  const base = cidr.split("/")[0].split(".").slice(0, 3).join(".");

  // Collect all used BMC IPs
  const usedIps = new Set<string>();
  for (const n of nodes) {
    if (n.type === "vmNode") {
      const ip = (n.data as Record<string, any>).bmcIp;
      if (ip) usedIps.add(ip);
    }
  }

  // Allocate from .11 upward (gateway is .1)
  for (let i = 11; i < 250; i++) {
    const candidate = `${base}.${i}`;
    if (!usedIps.has(candidate)) return candidate;
  }
  return `${base}.11`; // Fallback
}
```

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/stores/canvasStore.ts
git commit -m "feat: add BMC network auto-create/remove and IP allocation helpers"
```

---

## Task 8: Frontend — VM Properties Panel BMC Section

**Files:**
- Modify: `src/frontend/src/components/canvas/PropertiesPanel.tsx` — add BMC toggle and address display

- [ ] **Step 1: Add BMC section to VM properties**

In `PropertiesPanel.tsx`, after the Disk Controllers section (around line 861) and before the network node section, add a BMC section for VM nodes. Import the helpers at the top:

```typescript
import { syncBmcNetwork, allocateBmcIp } from "../../stores/canvasStore";
```

Then add the section inside the VM properties area (after disk controllers):

```tsx
{/* ── BMC (Baseboard Management Controller) ── */}
<div className="props-section">
  <div className="props-section-header" style={{ cursor: "pointer" }}
    onClick={() => toggleCollapse("bmc")}>
    <span style={{ transform: isCollapsed("bmc") ? "rotate(-90deg)" : undefined, display: "inline-block", transition: "transform 0.15s", marginRight: 4 }}>▾</span>
    BMC
  </div>
  {!isCollapsed("bmc") && (
    <div className="props-section-body">
      <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, cursor: "pointer", marginBottom: 8 }}>
        <input type="checkbox" checked={!!(node.data as Record<string, any>).bmcEnabled}
          disabled={projectState === "active" || projectState === "deploying"}
          onChange={(e) => {
            const enabled = e.target.checked;
            if (enabled) {
              const ip = allocateBmcIp();
              updateNodeData(node.id, { bmcEnabled: true, bmcIp: ip });
            } else {
              updateNodeData(node.id, { bmcEnabled: false, bmcIp: "" });
            }
            setTimeout(() => syncBmcNetwork(), 0);
          }}
        />
        Enable BMC
      </label>

      {(node.data as Record<string, any>).bmcEnabled && (
        <>
          <div className="props-field">
            <label className="props-label">BMC IP</label>
            <input className="props-input" value={(node.data as Record<string, any>).bmcIp || ""} readOnly
              style={{ fontFamily: "monospace", opacity: 0.7 }} />
          </div>

          {/* Show addresses when deployed */}
          {(() => {
            const deployedTopo = (window as any).__deployedTopology;
            const bmcData = deployedTopo?.bmc?.vms?.[node.id];
            if (!bmcData) return null;
            const bmcCreds = deployedTopo?.bmc;

            const CopyBtn = ({ value, label }: { value: string; label: string }) => (
              <button
                style={{ background: "none", border: "none", color: "var(--troshka-cyan)", cursor: "pointer", padding: 0, flexShrink: 0, opacity: 0.7, transition: "opacity 0.15s" }}
                onMouseEnter={(e) => (e.currentTarget.style.opacity = "1")}
                onMouseLeave={(e) => (e.currentTarget.style.opacity = "0.7")}
                title={`Copy ${label}`}
                onClick={(e) => {
                  navigator.clipboard.writeText(value);
                  const btn = e.currentTarget;
                  const orig = btn.innerHTML;
                  btn.innerHTML = `<span style="font-size:10px">Copied</span>`;
                  setTimeout(() => { btn.innerHTML = orig; }, 1000);
                }}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
              </button>
            );

            return (
              <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 4 }}>
                <div className="props-field">
                  <label className="props-label">Redfish URL</label>
                  <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                    <input className="props-input" value={bmcData.redfish_url} readOnly
                      style={{ fontFamily: "monospace", fontSize: 10, flex: 1 }} />
                    <CopyBtn value={bmcData.redfish_url} label="Redfish URL" />
                  </div>
                </div>
                <div className="props-field">
                  <label className="props-label">IPMI Address</label>
                  <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                    <input className="props-input" value={bmcData.ipmi_address} readOnly
                      style={{ fontFamily: "monospace", fontSize: 11, flex: 1 }} />
                    <CopyBtn value={bmcData.ipmi_address} label="IPMI address" />
                  </div>
                </div>
                <div className="props-field">
                  <label className="props-label">Username</label>
                  <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                    <input className="props-input" value={bmcCreds?.username || "admin"} readOnly
                      style={{ fontFamily: "monospace", fontSize: 11, flex: 1 }} />
                    <CopyBtn value={bmcCreds?.username || "admin"} label="username" />
                  </div>
                </div>
                <div className="props-field">
                  <label className="props-label">Password</label>
                  <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                    <input className="props-input" type="password" value={bmcCreds?.password || ""} readOnly
                      style={{ fontFamily: "monospace", fontSize: 11, flex: 1 }}
                      onFocus={(e) => (e.currentTarget.type = "text")}
                      onBlur={(e) => (e.currentTarget.type = "password")} />
                    <CopyBtn value={bmcCreds?.password || ""} label="password" />
                  </div>
                </div>
              </div>
            );
          })()}
        </>
      )}
    </div>
  )}
</div>
```

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/components/canvas/PropertiesPanel.tsx
git commit -m "feat: add BMC toggle and copy-to-clipboard address display in VM properties"
```

---

## Task 9: Frontend — BMC Network Node Rendering

**Files:**
- Modify: `src/frontend/src/components/canvas/nodes/NetworkNode.tsx` — add BMC network visual distinction and warning

- [ ] **Step 1: Add BMC network visual treatment**

In `NetworkNode.tsx`, the node card color is determined by subtype (lines 31-36). Add a check for BMC network type. Find the color selection logic and add:

```typescript
const networkType = (data as Record<string, any>).networkType;
const isBmc = networkType === "bmc";
```

Use a distinct color for BMC — purple/magenta to stand apart from regular cyan networks:

In the color logic, add before the default network case:

```typescript
const accentColor = isBmc ? "rgba(168,85,247,0.9)" : /* existing cyan logic */;
```

Add a visual indicator in the node body — after the CIDR display, show a "BMC" badge:

```tsx
{isBmc && (
  <span style={{ background: "rgba(168,85,247,0.2)", color: "rgba(168,85,247,1)", padding: "1px 6px", borderRadius: 4, fontSize: 9, fontWeight: 600 }}>
    BMC
  </span>
)}
```

- [ ] **Step 2: Add provisioner warning**

After the existing badges section, add a warning when the BMC network has no connected VMs:

```tsx
{isBmc && (() => {
  const edges = useCanvasStore.getState().edges;
  const hasConnection = edges.some((e) => e.source === id || e.target === id);
  return !hasConnection ? (
    <div style={{ background: "rgba(251,191,36,0.15)", color: "#fbbf24", fontSize: 9, padding: "3px 6px", borderRadius: 4, marginTop: 4, textAlign: "center" }}>
      ⚠ Connect a provisioner VM
    </div>
  ) : null;
})()}
```

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/components/canvas/nodes/NetworkNode.tsx
git commit -m "feat: add BMC network visual distinction and provisioner warning on canvas"
```

---

## Task 10: Frontend — BMC Network Properties Panel

**Files:**
- Modify: `src/frontend/src/components/canvas/PropertiesPanel.tsx` — add BMC network properties section

- [ ] **Step 1: Add BMC-specific fields to network properties**

In `PropertiesPanel.tsx`, find where network node properties are rendered (the section for `networkNode` type). Add BMC-specific fields when `networkType === "bmc"`:

```tsx
{(node.data as Record<string, any>).networkType === "bmc" && (
  <>
    <div className="props-field">
      <label className="props-label">BMC Username</label>
      <input className="props-input" value={(node.data as Record<string, any>).bmcUsername || "admin"}
        style={{ fontFamily: "monospace" }}
        onChange={(e) => update("bmcUsername", e.target.value)} />
    </div>
    <div className="props-field">
      <label className="props-label">BMC Password</label>
      <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
        <input className="props-input" type="password"
          value={(node.data as Record<string, any>).bmcPassword || ""}
          style={{ fontFamily: "monospace", flex: 1 }}
          onFocus={(e) => (e.currentTarget.type = "text")}
          onBlur={(e) => (e.currentTarget.type = "password")}
          onChange={(e) => update("bmcPassword", e.target.value)} />
      </div>
    </div>

    {/* List BMC-enabled VMs */}
    {(() => {
      const allNodes = useCanvasStore.getState().nodes;
      const bmcVms = allNodes.filter((n) => n.type === "vmNode" && (n.data as Record<string, any>).bmcEnabled);
      if (bmcVms.length === 0) return null;
      return (
        <div style={{ marginTop: 8 }}>
          <label className="props-label">BMC-Enabled VMs</label>
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            {bmcVms.map((vm) => (
              <div key={vm.id} style={{ fontSize: 11, fontFamily: "monospace", color: "var(--troshka-text-dim)", display: "flex", justifyContent: "space-between" }}>
                <span>{(vm.data as Record<string, any>).name || vm.id.slice(0, 8)}</span>
                <span>{(vm.data as Record<string, any>).bmcIp || "—"}</span>
              </div>
            ))}
          </div>
        </div>
      );
    })()}
  </>
)}
```

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/components/canvas/PropertiesPanel.tsx
git commit -m "feat: add BMC username/password fields and VM list to BMC network properties"
```

---

## Task 11: Frontend — Pass Deployed Topology to Properties Panel

**Files:**
- Modify: `src/frontend/src/app/projects/[id]/page.tsx` — pass BMC data from deployed topology to the properties panel

- [ ] **Step 1: Expose deployed topology BMC data**

The properties panel needs access to BMC addresses from `deployed_topology.bmc`. In `page.tsx`, after loading the project data (the `loadProject` function), store the BMC portion so the properties panel can access it:

```typescript
// After loading project data and setting topology
if (data.deployed_topology?.bmc) {
  (window as any).__deployedTopology = data.deployed_topology;
}
```

This is a lightweight approach — the properties panel already references `(window as any).__deployedTopology` in the BMC section from Task 8. A more structured approach (passing via props or context) could be done as a follow-up.

Also add cleanup when project state goes to "draft":

```typescript
if (data.state === "draft") {
  delete (window as any).__deployedTopology;
}
```

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/app/projects/[id]/page.tsx
git commit -m "feat: expose deployed topology BMC data to properties panel"
```

---

## Task 12: Frontend — Connection Validation for BMC Network

**Files:**
- Modify: `src/frontend/src/stores/canvasStore.ts:231-345` — update `onConnect` to handle BMC network edges

- [ ] **Step 1: Add BMC connection rules to `onConnect`**

In the `onConnect` handler in `canvasStore.ts`, find the connection validation section (around lines 236-294). Add a rule: only VM nodes (not other networks or storage) can connect to a BMC network:

After the existing network-to-network check (line 294):

```typescript
// BMC network: only VMs can connect (the provisioner)
const isBmcSource = sourceNode?.type === "networkNode" && (sourceNode.data as Record<string, any>).networkType === "bmc";
const isBmcTarget = targetNode?.type === "networkNode" && (targetNode.data as Record<string, any>).networkType === "bmc";
if ((isBmcSource || isBmcTarget) && (sourceNode?.type !== "vmNode" && targetNode?.type !== "vmNode")) {
  return;
}
```

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add src/frontend/src/stores/canvasStore.ts
git commit -m "feat: restrict BMC network connections to VM nodes only"
```

---

## Task 13: Backend API — Expose BMC Data in Project Response

**Files:**
- Modify: `src/backend/app/api/projects.py` — include BMC addresses in project detail response

- [ ] **Step 1: Add BMC data to project detail endpoint**

Find the project detail endpoint (GET `/{project_id}`). After returning the project data, include BMC info from `deployed_topology`:

```python
    # Include BMC addresses if available
    deployed_topo = project.deployed_topology or {}
    bmc_data = deployed_topo.get("bmc")
    if bmc_data:
        result["bmc"] = bmc_data
```

This ensures the frontend can load BMC addresses when opening an already-deployed project.

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/app/api/projects.py
git commit -m "feat: include BMC addresses in project detail API response"
```

---

## Task 14: CLAUDE.md Updates

**Files:**
- Modify: `CLAUDE.md` — add BMC conventions to the development guide

- [ ] **Step 1: Add BMC section to CLAUDE.md**

Add to the "Important Conventions" section in `CLAUDE.md`:

```markdown
### Virtual BMC (IPMI & Redfish)
- Per-VM BMC endpoints: one sushy-emulator + one vbmc per BMC-enabled VM
- BMC tools live in `/opt/troshka/venv/` (sushy-tools, virtualbmc, libvirt-python)
- BMC bridge: `br-bmc-{project_id[:8]}` inside project namespace
- BMC config: `/var/lib/troshka/bmc/{project_id}/` (sushy configs, vbmcd config, htpasswd)
- BMC network node: `networkType: "bmc"` on a networkNode, auto-created when first VM enables BMC
- Credentials stored in topology JSONB (preserved in patterns for lab instruction stability)
- Troshkad endpoints: `/bmc/setup`, `/bmc/teardown`, `/bmc/status`
- Deploy order: BMC setup runs after VM definition but before VM startup
```

Add to the "Host Operations" section:

```markdown
- BMC config: `/var/lib/troshka/bmc/{project_id}/` (sushy configs, vbmcd PID, htpasswd)
```

- [ ] **Step 2: Commit**

```bash
cd /Users/prutledg/troshka && git add CLAUDE.md
git commit -m "docs: add BMC conventions to CLAUDE.md"
```

---

## Task 15: Integration Test

**Files:**
- Create: `src/backend/tests/test_bmc_deploy.py`

- [ ] **Step 1: Write integration test for BMC config extraction**

```python
"""Tests for BMC configuration extraction from topology."""
import pytest
from app.services.deploy_service import _extract_bmc_config


def _make_topology(vms_with_bmc=None, include_bmc_network=True):
    """Build a minimal topology with optional BMC-enabled VMs."""
    nodes = []
    if include_bmc_network:
        nodes.append({
            "id": "bmc-net-1",
            "type": "networkNode",
            "data": {
                "name": "BMC Network",
                "subtype": "network",
                "networkType": "bmc",
                "cidr": "192.168.100.0/24",
                "dhcp": True,
                "dns": False,
                "bmcUsername": "admin",
                "bmcPassword": "testpass123",
            },
        })

    for i, (node_id, bmc_ip) in enumerate(vms_with_bmc or []):
        nodes.append({
            "id": node_id,
            "type": "vmNode",
            "data": {
                "name": f"vm-{i}",
                "bmcEnabled": True,
                "bmcIp": bmc_ip,
                "vcpus": 2,
                "ram": 4,
            },
        })

    return {"nodes": nodes, "edges": []}


def test_extract_bmc_config_with_vms():
    topo = _make_topology(vms_with_bmc=[
        ("aaaaaaaa-1111-1111-1111-111111111111", "192.168.100.11"),
        ("bbbbbbbb-2222-2222-2222-222222222222", "192.168.100.12"),
    ])
    result = _extract_bmc_config(topo, "cccccccc-3333-3333-3333-333333333333")
    assert result is not None
    assert len(result["vms"]) == 2
    assert result["vms"][0]["bmc_ip"] == "192.168.100.11"
    assert result["vms"][0]["domain_name"] == "troshka-cccccccc-aaaaaaaa"
    assert result["bmc_network"]["bmcPassword"] == "testpass123"


def test_extract_bmc_config_no_bmc_network():
    topo = _make_topology(include_bmc_network=False)
    result = _extract_bmc_config(topo, "cccccccc-3333-3333-3333-333333333333")
    assert result is None


def test_extract_bmc_config_no_bmc_vms():
    topo = _make_topology(vms_with_bmc=[])
    result = _extract_bmc_config(topo, "cccccccc-3333-3333-3333-333333333333")
    assert result is None
```

- [ ] **Step 2: Run tests**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/test_bmc_deploy.py -v
```

Expected: All 3 tests PASS.

- [ ] **Step 3: Commit**

```bash
cd /Users/prutledg/troshka && git add src/backend/tests/test_bmc_deploy.py
git commit -m "test: add BMC config extraction tests"
```

---

## Task 16: Manual Verification Checklist

This is a hands-on verification task, not a code task.

- [ ] **Step 1: Verify agent installer changes**

Review `agent_deployer.py` — confirm `python3-devel pkg-config gcc` are in DNF list and venv creation is in the install script.

- [ ] **Step 2: Test canvas BMC toggle flow**

1. Start dev services: `./dev-services.sh start`
2. Open http://localhost:3100, create a project
3. Add 3 VMs and a network
4. Enable BMC on 2 VMs — verify BMC network appears on canvas
5. Verify BMC IPs are assigned (192.168.100.11, 192.168.100.12)
6. Verify BMC network shows "Connect a provisioner VM" warning
7. Connect the 3rd VM to the BMC network — warning disappears
8. Disable BMC on both VMs — BMC network auto-removes
9. Re-enable BMC — BMC network reappears with new password

- [ ] **Step 3: Test deploy validation**

1. Enable BMC on a VM — BMC network appears
2. Do NOT connect any VM to BMC network
3. Try to deploy — should get error: "BMC network requires at least one connected VM to act as a provisioner"

- [ ] **Step 4: Test BMC properties panel**

1. Click BMC network node — verify CIDR, username, password fields
2. Verify password is masked, reveals on focus
3. Verify BMC-enabled VMs listed with IPs

- [ ] **Step 5: Test pattern preservation**

1. Create a project with BMC-enabled VMs
2. Save as pattern
3. Create new project from pattern
4. Verify BMC password is the same as the original
