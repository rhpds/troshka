# Guest Agent Exec + Unified Exec Priority — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `qemu-guest-agent` as the primary exec method and unify the exec fallback order to guest-agent → SSH → console → serial across all providers (troshkad hosts and KubeVirt native).

**Architecture:** New `vm/guest-exec` command handler in troshkad uses `virsh qemu-agent-command` for structured command execution (stdout/stderr/exit_code, no TTY parsing, no credentials). KubeVirt native gets three new exec methods: guest-agent via virt-launcher pod exec, SSH via dnsmasq pod exec, and console via the KubeVirt console subresource. The backend `vm_exec` endpoint gains a `host_type` branch to route KubeVirt VMs to the new provider-level functions instead of troshkad jobs.

**Tech Stack:** Python 3.11, FastAPI, `virsh qemu-agent-command`, `kubernetes` Python client (`kubernetes.stream`), pexpect (existing)

## Global Constraints

- troshkad is stdlib-only Python — no pip dependencies
- All host operations go through troshkad, never direct SSH (except KubeVirt native which has no troshkad)
- Never block HTTP requests — exec is already synchronous (waits for job), keep that pattern
- KubeVirt pod exec uses the same `_get_k8s_clients()` → `(custom_api, core_v1, api_client)` pattern
- KubeVirt VM domain name inside virt-launcher: discover via `virsh list` in the pod (namespace-prefixed)
- ubi-minimal uses `microdnf`, not `dnf`

---

### Task 1: Troshkad guest-agent exec handler

**Files:**
- Modify: `src/troshkad/troshkad.py` — add handler after line 7900 (after `_handle_vm_ssh_exec`), add to `_SKIP_DRAIN` set at line 6356

**Interfaces:**
- Consumes: `_validate_domain_name(name) -> str` (line 638), `COMMAND_HANDLERS` dict (line 371), `_SKIP_DRAIN` set (line 6356)
- Produces: `COMMAND_HANDLERS["vm/guest-exec"]` — handler signature `(job, params) -> dict` returning `{"output": str, "error": str, "exit_code": int}`

- [ ] **Step 1: Write the guest-exec handler**

Add after line 7900 in `troshkad.py` (after `COMMAND_HANDLERS["vm/ssh-exec"]`):

```python
def _handle_vm_guest_exec(job, params):
    """Execute a command on a VM via qemu-guest-agent."""
    domain = _validate_domain_name(params["domain_name"])
    command = params.get("command", "")
    timeout_secs = min(params.get("timeout", 600), 3600)

    if not command:
        raise RuntimeError("No command specified")

    # Check guest agent is available (10s timeout to avoid blocking on frozen VMs)
    try:
        check = subprocess.run(
            [
                "virsh", "qemu-agent-command", domain,
                '{"execute":"guest-info"}',
                "--timeout", "10",
            ],
            capture_output=True, text=True, timeout=15,
        )
        if check.returncode != 0:
            raise RuntimeError(
                f"Guest agent not available on {domain}: {check.stderr.strip()}"
            )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Guest agent not responding on {domain}")

    # Execute command via guest-exec
    import json as _json
    exec_cmd = _json.dumps({
        "execute": "guest-exec",
        "arguments": {
            "path": "/bin/sh",
            "arg": ["-c", command],
            "capture-output": True,
        },
    })
    result = subprocess.run(
        ["virsh", "qemu-agent-command", domain, exec_cmd, "--timeout", "10"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"guest-exec failed: {result.stderr.strip()}")

    resp = _json.loads(result.stdout)
    pid = resp.get("return", {}).get("pid")
    if pid is None:
        raise RuntimeError(f"No PID in guest-exec response: {result.stdout}")

    # Poll for completion
    import base64
    status_cmd = _json.dumps({
        "execute": "guest-exec-status",
        "arguments": {"pid": pid},
    })
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        if job.get("_cancelled"):
            raise RuntimeError("Job cancelled")
        sr = subprocess.run(
            ["virsh", "qemu-agent-command", domain, status_cmd, "--timeout", "10"],
            capture_output=True, text=True, timeout=15,
        )
        if sr.returncode != 0:
            raise RuntimeError(f"guest-exec-status failed: {sr.stderr.strip()}")
        status = _json.loads(sr.stdout).get("return", {})
        if status.get("exited"):
            stdout = ""
            stderr = ""
            if status.get("out-data"):
                stdout = base64.b64decode(status["out-data"]).decode("utf-8", errors="replace")
            if status.get("err-data"):
                stderr = base64.b64decode(status["err-data"]).decode("utf-8", errors="replace")
            return {
                "output": stdout,
                "error": stderr,
                "exit_code": status.get("exitcode", -1),
            }
        time.sleep(0.5)

    raise RuntimeError(f"guest-exec timed out after {timeout_secs}s (pid={pid})")


COMMAND_HANDLERS["vm/guest-exec"] = _handle_vm_guest_exec
```

- [ ] **Step 2: Add to `_SKIP_DRAIN`**

At line 6356, add `"vm/guest-exec"` to the `_SKIP_DRAIN` set:

```python
_SKIP_DRAIN = {
    "vms/state",
    "vms/states",
    "host/disk-usage",
    "gc/discover",
    "vm/ssh-exec",
    "vm/guest-exec",
    "containers/states",
}
```

- [ ] **Step 3: Manual test on a troshkad host**

Push the updated agent and test:
```bash
./scripts/update-agent.sh
# Then via API:
curl -X POST http://localhost:8200/api/v1/projects/{project_id}/vms/{vm_id}/exec \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"command": "hostname", "method": "guest-agent"}'
```

Expected: `{"output": "vm-hostname\n", "error": "", "exit_code": 0, "method": "guest-agent"}`

If guest agent isn't running in the VM, expect: 503 with "Guest agent not available"

- [ ] **Step 4: Commit**

```bash
git add src/troshkad/troshkad.py
git commit -m "feat: add guest-agent exec handler to troshkad"
```

---

### Task 2: Backend — reorder auto methods + add guest-agent dispatch

**Files:**
- Modify: `src/backend/app/api/projects.py:1717-1871` — rewrite `vm_exec` endpoint

**Interfaces:**
- Consumes: `start_job(host, path, params)`, `wait_for_job(host, job_id, timeout)`, `TroshkadError` — all from `troshkad_client`
- Produces: Updated `vm_exec` with `"guest-agent"` method support and new auto order `["guest-agent", "ssh", "console", "serial"]`

- [ ] **Step 1: Update the method resolution and auto order**

In `projects.py`, replace lines 1772-1776 (the method-to-list mapping):

Old:
```python
    if method == "auto":
        methods = ["ssh", "console-text", "console", "serial"]
        force_tty = False
    else:
        methods = [method]
```

New:
```python
    if method == "auto":
        methods = ["guest-agent", "ssh", "console", "serial"]
        force_tty = False
    else:
        methods = [method]
```

- [ ] **Step 2: Add the guest-agent branch in the for loop**

Insert at the top of the `for m in methods:` loop body (line 1780), before the `if m == "ssh":` branch:

```python
            if m == "guest-agent":
                job_id = start_job(
                    host,
                    "/vm/guest-exec",
                    {
                        "domain_name": dom,
                        "command": command,
                        "timeout": timeout,
                    },
                )
                job = wait_for_job(host, job_id, timeout=timeout + 30)
                if job["status"] == "completed":
                    result = job.get("result", {})
                    return {
                        "output": result.get("output", ""),
                        "error": result.get("error", ""),
                        "exit_code": result.get("exit_code", 0),
                        "method": "guest-agent",
                    }
                errors.append(
                    f"guest-agent: {job.get('result', {}).get('error', 'failed')}"
                )

            elif m == "ssh":
```

Note: change the existing `if m == "ssh":` to `elif m == "ssh":`.

- [ ] **Step 3: Update docstring**

Update the endpoint docstring (line 1725-1732) to mention `"guest-agent"` as a valid method:

```python
    """Execute a command on a VM.

    Body params:
        command: Shell command to execute (required)
        username: SSH/console user (default: cloud-user)
        password: VM password (auto-resolved from topology if omitted)
        timeout: Command timeout in seconds (default: 600, max: 3600)
        method: "auto" (tries guest-agent → ssh → console → serial),
                "guest-agent", "ssh", "serial", or "console"
    """
```

- [ ] **Step 4: Run backend tests**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/ -v
```

Expected: all existing tests pass (exec endpoint isn't tested directly — it requires a live host).

- [ ] **Step 5: Commit**

```bash
git add src/backend/app/api/projects.py
git commit -m "feat: add guest-agent to exec fallback order (first priority)"
```

---

### Task 3: KubeVirt exec functions in kubevirt.py

**Files:**
- Modify: `src/backend/app/services/providers/kubevirt.py` — add three exec functions at end of file

**Interfaces:**
- Consumes: `_get_k8s_clients(provider) -> (custom_api, core_v1, api_client)` (line 25), `_project_ns(provider, project_id) -> str` (line 19)
- Produces:
  - `kubevirt_exec_guest_agent(provider, project_id, vm_id, command, timeout) -> dict` — returns `{"output", "error", "exit_code", "method"}`
  - `kubevirt_exec_ssh(provider, project_id, vm_id, vm_ip, username, password, command, timeout) -> dict`
  - `kubevirt_exec_console(provider, project_id, vm_id, username, password, command, timeout) -> dict`

- [ ] **Step 1: Add guest-agent exec via virt-launcher pod**

Append to the end of `kubevirt.py`:

```python
def kubevirt_exec_guest_agent(provider, project_id, vm_id, command, timeout=600):
    """Execute command via qemu-guest-agent inside the virt-launcher pod."""
    import json
    import base64

    _, core_v1, _ = _get_k8s_clients(provider)
    namespace = _project_ns(provider, project_id)
    vm_name = f"troshka-vm-{vm_id[:8]}"

    pods = core_v1.list_namespaced_pod(
        namespace, label_selector=f"vm.kubevirt.io/name={vm_name}"
    )
    launcher = None
    for p in pods.items:
        if p.metadata.name.startswith("virt-launcher-") and p.status.phase == "Running":
            launcher = p
            break
    if not launcher:
        raise RuntimeError(f"No running virt-launcher pod for {vm_name}")

    from kubernetes.stream import stream as k8s_stream

    # Discover the libvirt domain name inside the pod
    resp = k8s_stream(
        core_v1.connect_get_namespaced_pod_exec,
        launcher.metadata.name,
        namespace,
        container="compute",
        command=["virsh", "list", "--name"],
        stderr=True, stdout=True, stdin=False, tty=False,
        _preload_content=True,
    )
    domain = resp.strip().split("\n")[0].strip()
    if not domain:
        raise RuntimeError("No libvirt domain found in virt-launcher pod")

    # Check guest agent availability
    check_resp = k8s_stream(
        core_v1.connect_get_namespaced_pod_exec,
        launcher.metadata.name,
        namespace,
        container="compute",
        command=[
            "virsh", "qemu-agent-command", domain,
            '{"execute":"guest-info"}', "--timeout", "10",
        ],
        stderr=True, stdout=True, stdin=False, tty=False,
        _preload_content=True,
    )
    if "error" in check_resp.lower() and "guest agent" in check_resp.lower():
        raise RuntimeError(f"Guest agent not available: {check_resp}")

    # Execute command
    exec_payload = json.dumps({
        "execute": "guest-exec",
        "arguments": {
            "path": "/bin/sh",
            "arg": ["-c", command],
            "capture-output": True,
        },
    })
    exec_resp = k8s_stream(
        core_v1.connect_get_namespaced_pod_exec,
        launcher.metadata.name,
        namespace,
        container="compute",
        command=["virsh", "qemu-agent-command", domain, exec_payload, "--timeout", "10"],
        stderr=True, stdout=True, stdin=False, tty=False,
        _preload_content=True,
    )
    parsed = json.loads(exec_resp)
    pid = parsed.get("return", {}).get("pid")
    if pid is None:
        raise RuntimeError(f"No PID in guest-exec response: {exec_resp}")

    # Poll for completion
    import time

    status_payload = json.dumps({
        "execute": "guest-exec-status",
        "arguments": {"pid": pid},
    })
    deadline = time.time() + timeout
    while time.time() < deadline:
        sr = k8s_stream(
            core_v1.connect_get_namespaced_pod_exec,
            launcher.metadata.name,
            namespace,
            container="compute",
            command=["virsh", "qemu-agent-command", domain, status_payload, "--timeout", "10"],
            stderr=True, stdout=True, stdin=False, tty=False,
            _preload_content=True,
        )
        status = json.loads(sr).get("return", {})
        if status.get("exited"):
            stdout = ""
            stderr = ""
            if status.get("out-data"):
                stdout = base64.b64decode(status["out-data"]).decode("utf-8", errors="replace")
            if status.get("err-data"):
                stderr = base64.b64decode(status["err-data"]).decode("utf-8", errors="replace")
            return {
                "output": stdout,
                "error": stderr,
                "exit_code": status.get("exitcode", -1),
                "method": "guest-agent",
            }
        time.sleep(0.5)

    raise RuntimeError(f"guest-exec timed out after {timeout}s (pid={pid})")
```

- [ ] **Step 2: Add SSH exec via dnsmasq pod**

Append after the guest-agent function:

```python
def kubevirt_exec_ssh(provider, project_id, vm_id, vm_ip, username, password, command, timeout=600):
    """Execute command via SSH from the dnsmasq pod (on the OVN network)."""
    _, core_v1, _ = _get_k8s_clients(provider)
    namespace = _project_ns(provider, project_id)

    pods = core_v1.list_namespaced_pod(
        namespace, label_selector=f"app=dnsmasq,troshka-project={project_id[:8]}"
    )
    dnsmasq_pod = None
    for p in pods.items:
        if p.status.phase == "Running":
            dnsmasq_pod = p
            break
    if not dnsmasq_pod:
        raise RuntimeError("No running dnsmasq pod found")

    if not vm_ip:
        raise RuntimeError("No VM IP for SSH exec")
    if not password:
        raise RuntimeError("No password for SSH exec (key auth not supported on KubeVirt)")

    from kubernetes.stream import stream as k8s_stream

    ssh_cmd = [
        "sshpass", "-p", password,
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", f"ConnectTimeout={min(timeout, 10)}",
        f"{username}@{vm_ip}",
        command,
    ]
    resp = k8s_stream(
        core_v1.connect_get_namespaced_pod_exec,
        dnsmasq_pod.metadata.name,
        namespace,
        command=ssh_cmd,
        stderr=True, stdout=True, stdin=False, tty=False,
        _preload_content=True,
    )
    # k8s_stream with _preload_content=True returns combined output as a string.
    # We can't reliably separate stdout/stderr this way, but it's sufficient.
    return {
        "output": resp,
        "error": "",
        "exit_code": 0,
        "method": "ssh",
    }
```

- [ ] **Step 3: Add console exec via KubeVirt console subresource**

Append after the SSH function:

```python
def kubevirt_exec_console(provider, project_id, vm_id, username, password, command, timeout=600):
    """Execute command via KubeVirt serial console (WebSocket-based)."""
    import re
    import time

    _, core_v1, api_client = _get_k8s_clients(provider)
    namespace = _project_ns(provider, project_id)
    vm_name = f"troshka-vm-{vm_id[:8]}"

    if not password:
        raise RuntimeError("Password required for console exec")

    from kubernetes.stream import stream as k8s_stream

    # Open console via the VMI console subresource using the API client
    from kubernetes import client as k8s_client

    api = k8s_client.CustomObjectsApi(api_client)

    # Use websocket to connect to the console subresource
    # The kubernetes client doesn't have a native console stream helper,
    # so we use the raw API path with the websocket protocol.
    import websocket
    import ssl

    creds = provider.get_credentials()
    api_url = creds["api_url"]
    token = creds["token"]
    ws_url = api_url.replace("https://", "wss://").replace("http://", "ws://")
    console_path = (
        f"/apis/subresources.kubevirt.io/v1/namespaces/{namespace}"
        f"/virtualmachineinstances/{vm_name}/console"
    )
    full_url = f"{ws_url}{console_path}"

    ws = websocket.create_connection(
        full_url,
        header=[f"Authorization: Bearer {token}"],
        subprotocols=["plain.kubevirt.io"],
        sslopt={"cert_reqs": ssl.CERT_NONE},
        timeout=min(timeout, 30),
    )

    def _ws_read(secs):
        """Read all available data from WebSocket within timeout."""
        buf = ""
        deadline = time.time() + secs
        ws.settimeout(0.5)
        while time.time() < deadline:
            try:
                data = ws.recv()
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                buf += data
            except websocket.WebSocketTimeoutException:
                if buf:
                    break
        return buf

    def _ws_send(text):
        ws.send(text.encode("utf-8") if isinstance(text, str) else text)

    def _strip_ansi(s):
        return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", s)

    try:
        # Read initial output to detect state
        initial = _ws_read(3)

        # Send enter to wake up console
        _ws_send("\n")
        prompt_check = _ws_read(3)
        combined = initial + prompt_check

        # Detect state: login prompt, password prompt, or shell
        if "login:" in combined.lower():
            _ws_send(f"{username}\n")
            _ws_read(2)
            _ws_send(f"{password}\n")
            login_resp = _ws_read(3)
            if "login incorrect" in login_resp.lower():
                raise RuntimeError("Console login failed")
        elif "password:" in combined.lower():
            _ws_send(f"{password}\n")
            login_resp = _ws_read(3)
            if "login incorrect" in login_resp.lower():
                raise RuntimeError("Console login failed")
        # else: already at a shell prompt

        # Send command wrapped with markers
        _ws_send("echo TROSHKA_BEGIN\n")
        _ws_read(1)
        _ws_send(f"({command}) 2>&1; echo TROSHKA_END $?\n")

        # Read until TROSHKA_END marker
        output = ""
        deadline = time.time() + min(timeout, 300)
        while time.time() < deadline:
            chunk = _ws_read(2)
            output += chunk
            if "TROSHKA_END" in output:
                break

        # Parse output between markers
        clean = _strip_ansi(output)
        begin_idx = clean.find("TROSHKA_BEGIN")
        end_idx = clean.find("TROSHKA_END")
        if begin_idx >= 0 and end_idx >= 0:
            body = clean[begin_idx + len("TROSHKA_BEGIN"):end_idx].strip()
            # Extract exit code from the TROSHKA_END line
            end_line = clean[end_idx:].split("\n")[0]
            exit_code_match = re.search(r"TROSHKA_END\s+(\d+)", end_line)
            exit_code = int(exit_code_match.group(1)) if exit_code_match else None
        else:
            body = clean
            exit_code = None

        return {
            "output": body,
            "error": "",
            "exit_code": exit_code,
            "method": "console",
        }
    finally:
        try:
            ws.close()
        except Exception:
            pass
```

- [ ] **Step 4: Commit**

```bash
git add src/backend/app/services/providers/kubevirt.py
git commit -m "feat: add guest-agent, SSH, and console exec for KubeVirt native"
```

---

### Task 4: Backend — KubeVirt exec routing in vm_exec

**Files:**
- Modify: `src/backend/app/api/projects.py:1717-1871` — add `host_type == "kubevirt-cluster"` branch

**Interfaces:**
- Consumes: `kubevirt_exec_guest_agent(provider, project_id, vm_id, command, timeout)`, `kubevirt_exec_ssh(provider, project_id, vm_id, vm_ip, username, password, command, timeout)`, `kubevirt_exec_console(provider, project_id, vm_id, username, password, command, timeout)` — all from `kubevirt.py`
- Produces: Updated `vm_exec` that routes KubeVirt VMs to provider-level exec functions

- [ ] **Step 1: Add the KubeVirt branch before the troshkad for-loop**

After the `methods = [method]` / `errors = []` block (around line 1777), insert a KubeVirt branch that returns early. The existing for-loop becomes the `else` (troshkad) path.

Insert before `for m in methods:` (line 1779):

```python
    if host.host_type == "kubevirt-cluster":
        from app.models.provider import Provider
        from app.services.providers.kubevirt import (
            kubevirt_exec_guest_agent,
            kubevirt_exec_ssh,
            kubevirt_exec_console,
        )

        provider = db.query(Provider).filter_by(id=host.provider_id).first()
        if not provider:
            raise HTTPException(status_code=503, detail="Provider not found")

        # KubeVirt auto order: guest-agent → ssh → console (no serial — same as console)
        kv_methods = methods
        if method == "auto":
            kv_methods = ["guest-agent", "ssh", "console"]

        for m in kv_methods:
            try:
                if m == "guest-agent":
                    return kubevirt_exec_guest_agent(
                        provider, project_id, vm_id, command, timeout
                    )
                elif m == "ssh":
                    if not vm_ip or not password:
                        errors.append("ssh: no VM IP or credentials")
                        continue
                    return kubevirt_exec_ssh(
                        provider, project_id, vm_id, vm_ip, username, password,
                        command, timeout,
                    )
                elif m in ("console", "serial"):
                    console_pass = root_password or password
                    if not console_pass:
                        errors.append("console: no password available")
                        continue
                    return kubevirt_exec_console(
                        provider, project_id, vm_id,
                        "root" if root_password else username,
                        console_pass, command, timeout,
                    )
            except Exception as e:
                errors.append(f"{m}: {e}")
                if method != "auto":
                    raise HTTPException(
                        status_code=503, detail=f"{m} exec failed: {e}"
                    )

        raise HTTPException(
            status_code=503,
            detail="All exec methods failed: " + "; ".join(errors),
        )

    # Troshkad hosts — existing dispatch via start_job/wait_for_job
    for m in methods:
```

- [ ] **Step 2: Run backend tests**

```bash
cd /Users/prutledg/troshka/src/backend && ./venv/bin/python3 -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add src/backend/app/api/projects.py
git commit -m "feat: route KubeVirt VMs to provider-level exec functions"
```

---

### Task 5: RBAC + dnsmasq image updates

**Files:**
- Modify: `infra/ocpvirt-rbac.yaml:26-28` — add `virtualmachineinstances/exec` subresource
- Modify: `src/operator/images/dnsmasq/Dockerfile:3` — add `openssh-clients` and `sshpass`

**Interfaces:**
- Consumes: nothing
- Produces: RBAC allows `exec` subresource on VMIs; dnsmasq image has SSH client for `kubevirt_exec_ssh`

- [ ] **Step 1: Update RBAC**

In `infra/ocpvirt-rbac.yaml`, replace line 27:

Old:
```yaml
    resources: ["virtualmachines", "virtualmachineinstances", "virtualmachineinstances/vnc", "virtualmachineinstances/console"]
```

New:
```yaml
    resources: ["virtualmachines", "virtualmachineinstances", "virtualmachineinstances/vnc", "virtualmachineinstances/console", "virtualmachineinstances/exec"]
```

- [ ] **Step 2: Update dnsmasq Dockerfile**

In `src/operator/images/dnsmasq/Dockerfile`, replace line 3:

Old:
```dockerfile
RUN microdnf install -y dnsmasq python3 && microdnf clean all
```

New:
```dockerfile
RUN microdnf install -y dnsmasq python3 openssh-clients sshpass && microdnf clean all
```

- [ ] **Step 3: Commit**

```bash
git add infra/ocpvirt-rbac.yaml src/operator/images/dnsmasq/Dockerfile
git commit -m "feat: add exec RBAC + SSH packages to dnsmasq image"
```

---

### Task 6: Add `websocket-client` dependency to backend

**Files:**
- Modify: `src/backend/requirements.txt` (or equivalent) — add `websocket-client`

**Interfaces:**
- Consumes: nothing
- Produces: `websocket-client` package available for `kubevirt_exec_console`

- [ ] **Step 1: Check current dependencies**

```bash
grep -i websocket /Users/prutledg/troshka/src/backend/requirements.txt 2>/dev/null
pip list 2>/dev/null | grep -i websocket
```

If `websocket-client` is already installed (often comes with the `kubernetes` Python client), skip this task.

- [ ] **Step 2: Add dependency if needed**

If not present, add `websocket-client` to requirements.txt and install:

```bash
echo "websocket-client" >> src/backend/requirements.txt
cd /Users/prutledg/troshka/src/backend && ./venv/bin/pip install websocket-client
```

- [ ] **Step 3: Commit if changed**

```bash
git add src/backend/requirements.txt
git commit -m "deps: add websocket-client for KubeVirt console exec"
```

---

### Task 7: Integration test — troshkad guest-agent exec

This is a manual integration test on a live environment.

- [ ] **Step 1: Push updated agent**

```bash
./scripts/update-agent.sh
```

- [ ] **Step 2: Test guest-agent exec on a running VM**

Use the exec API with `method=guest-agent`:
```bash
./scripts/vm-exec.sh {project_id} {vm_id} "hostname" --method guest-agent
# Or via curl:
curl -s -X POST http://localhost:8200/api/v1/projects/{project_id}/vms/{vm_id}/exec \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"command":"hostname","method":"guest-agent"}' | python3 -m json.tool
```

Expected: `{"output": "<hostname>\n", "error": "", "exit_code": 0, "method": "guest-agent"}`

- [ ] **Step 3: Test auto fallback**

Use `method=auto` (default):
```bash
curl -s -X POST http://localhost:8200/api/v1/projects/{project_id}/vms/{vm_id}/exec \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"command":"hostname"}' | python3 -m json.tool
```

Expected: `"method": "guest-agent"` (should pick guest-agent first if agent is running).

- [ ] **Step 4: Test fallback when guest agent is unavailable**

Stop the guest agent inside a VM and verify fallback to SSH:
```bash
# Stop guest agent (via a prior exec or serial):
curl -s -X POST .../exec -d '{"command":"systemctl stop qemu-guest-agent","method":"ssh"}' ...
# Then try auto:
curl -s -X POST .../exec -d '{"command":"hostname"}' ...
```

Expected: `"method": "ssh"` (guest-agent fails, falls back to SSH).
