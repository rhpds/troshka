# Guest Agent Exec + Unified Exec Priority

## Goal

Add `qemu-guest-agent` as the primary exec method across all providers, and unify the exec fallback order to: **guest-agent → SSH → console → serial**.

## Background

The exec endpoint (`POST /projects/{id}/vms/{vm_id}/exec`) currently supports `auto`, `ssh`, `serial`, and `console` methods. All dispatch to troshkad via `start_job`/`wait_for_job`. The auto order is `ssh → console-text → console → serial`.

Guest-agent exec is superior to all existing methods: structured stdout/stderr/exit_code with no TTY parsing, no network required, no credentials required. It works as long as `qemu-guest-agent` is running in the VM (standard on RHEL, available via ignition on RHCOS).

KubeVirt native has no exec implementation at all.

## Unified Auto Priority Order

1. **Guest agent** — `virsh qemu-agent-command` (troshkad) or virt-launcher pod exec (KubeVirt). Structured output, no credentials, no network.
2. **SSH** — SSH to VM IP via project network namespace (troshkad) or dnsmasq pod exec (KubeVirt). Requires network + credentials.
3. **Console** — VNC send-key + OCR (troshkad) or KubeVirt console subresource with pexpect (KubeVirt). Requires credentials.
4. **Serial** — PTY pexpect via `virsh dumpxml` (troshkad only). Unreliable for complex commands. Last resort.

## Implementation

### 1. Troshkad: `vm/guest-exec` handler

New command handler in `troshkad.py`:

1. Look up domain name from params (same `_validate_domain_name` pattern).
2. Check agent availability: `virsh qemu-agent-command <domain> '{"execute":"guest-info"}'`. If this fails (timeout, error), raise `RuntimeError` so the backend falls back.
3. Execute command: `virsh qemu-agent-command <domain> '{"execute":"guest-exec","arguments":{"path":"/bin/sh","arg":["-c","<command>"],"capture-output":true}}'`. Parse response for PID.
4. Poll `guest-exec-status` with the PID until `exited: true` or timeout. Poll interval: 0.5s.
5. Base64-decode `out-data` and `err-data`, return `{"output": stdout, "error": stderr, "exit_code": exitcode}`.

Timeout: use the `timeout` param from the request (default 600, max 3600). The `virsh qemu-agent-command` itself gets a 10s timeout for the initial call; polling has the full timeout budget.

Register as `COMMAND_HANDLERS["vm/guest-exec"]`.

Add to `_SKIP_DRAIN` so guest-exec doesn't cancel agent update drain (same as `vm/ssh-exec`).

### 2. Backend: Reorder auto methods + add guest-agent

In `projects.py` `vm_exec`:

- Add `"guest-agent"` as a valid `method` value.
- Change auto order from `["ssh", "console-text", "console", "serial"]` to `["guest-agent", "ssh", "console", "serial"]`. Drop `console-text` as a separate entry (it was a workaround; console handles both modes).
- For `method="guest-agent"` on troshkad hosts: `start_job(host, "/vm/guest-exec", {"domain_name": dom, "command": command, "timeout": timeout})`.
- Guest-agent exec does not need username, password, private_key, or vm_ip — only domain_name and command.

### 3. Backend: KubeVirt exec branch

In `vm_exec`, when `host.host_type == "kubevirt-cluster"`:

Route to new functions in `kubevirt.py` instead of troshkad jobs. Each method is a standalone function that returns `{"output", "error", "exit_code", "method"}` or raises an exception to trigger fallback.

#### 3a. KubeVirt guest-agent exec

Find the virt-launcher pod for the VM:
```python
pods = core_v1.list_namespaced_pod(namespace, label_selector=f"vm.kubevirt.io/name={vm_name}")
launcher_pod = next(p for p in pods.items if p.metadata.name.startswith("virt-launcher-"))
```

Exec into it and run `virsh qemu-agent-command`:
```python
from kubernetes.stream import stream
resp = stream(core_v1.connect_get_namespaced_pod_exec,
    launcher_pod.metadata.name, namespace,
    container="compute",
    command=["virsh", "qemu-agent-command", vm_domain,
             json.dumps({"execute": "guest-exec", "arguments": {
                 "path": "/bin/sh", "arg": ["-c", command],
                 "capture-output": True}})],
    stderr=True, stdin=False, stdout=True, tty=False)
```

Parse the JSON response, then poll `guest-exec-status` the same way. The virt-launcher pod has the libvirt socket and `virsh` binary.

VM domain name inside virt-launcher: the KubeVirt domain name is the namespace + VMI name. Use `virsh list` inside the pod to discover it if needed.

#### 3b. KubeVirt SSH exec

Find the dnsmasq pod for the project:
```python
pods = core_v1.list_namespaced_pod(namespace, label_selector=f"app=dnsmasq,troshka-project={project_id[:8]}")
```

Exec into it and run SSH (dnsmasq pod is on the OVN secondary network and can reach VM IPs):
```python
ssh_cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
           f"{username}@{vm_ip}", command]
# If using password: prepend sshpass
# If using key: write to temp file inside pod first
```

For password auth, the dnsmasq image needs `sshpass`. If not available, skip SSH fallback (guest-agent should handle most cases). SSH key auth: write key to stdin via the exec stream, or use `ssh -o StrictHostKeyChecking=no` with a heredoc.

Simpler approach: only support password-based SSH via dnsmasq pod for now (install `openssh-clients` and `sshpass` in the dnsmasq image). Key-based SSH can be added later.

#### 3c. KubeVirt console exec

Use the KubeVirt console subresource (WebSocket-based serial console):
```
/apis/subresources.kubevirt.io/v1/namespaces/{ns}/virtualmachineinstances/{name}/console
```

This is a WebSocket stream equivalent to `virsh console`. Implement pexpect-style interaction:
1. Open WebSocket connection to the console subresource.
2. Detect state (login prompt vs shell) by reading initial output.
3. Login if needed (send username + password).
4. Send command wrapped with markers: `echo TROSHKA_BEGIN; {command} 2>&1; echo TROSHKA_END $?`
5. Read until `TROSHKA_END` marker appears. Parse output and exit code.
6. Close WebSocket.

Use the `kubernetes` Python client's WebSocket support or raw `websocket-client`. Timeout handling via `select()` on the WebSocket.

This is the same prompt-parsing approach as troshkad's `_handle_vm_serial_exec`, adapted to a WebSocket transport instead of a local PTY fd.

#### 3d. KubeVirt serial

On KubeVirt, serial and console are the same subresource — there's only one text channel. If console exec failed, serial would fail too. Map `serial` to the same console subresource handler (or skip it in the KubeVirt auto list).

Effective KubeVirt auto order: `["guest-agent", "ssh", "console"]` (serial omitted since it's identical to console).

### 4. RBAC

Add to `infra/ocpvirt-rbac.yaml` under the `subresources.kubevirt.io` rule:
```yaml
- apiGroups: ["subresources.kubevirt.io"]
  resources:
    - virtualmachines
    - virtualmachineinstances
    - virtualmachineinstances/vnc
    - virtualmachineinstances/console
    - virtualmachineinstances/exec    # new
  verbs: ["get"]
```

The `exec` subresource is for future-proofing — KubeVirt may expose guest-exec as a first-class subresource. For now we go through virt-launcher pod exec.

### 5. Dnsmasq image update

Add `openssh-clients` and `sshpass` to the dnsmasq container image so SSH exec works from inside the pod.

File: `src/operator/images/dnsmasq/Containerfile` — add to the `dnf install` line.

## Files to modify

| File | Change |
|------|--------|
| `src/troshkad/troshkad.py` | Add `vm/guest-exec` handler + `_SKIP_DRAIN` entry |
| `src/backend/app/api/projects.py` | Reorder auto methods, add guest-agent dispatch, add KubeVirt exec branch |
| `src/backend/app/services/providers/kubevirt.py` | Add `exec_guest_agent()`, `exec_ssh()`, `exec_console()` |
| `infra/ocpvirt-rbac.yaml` | Add `virtualmachineinstances/exec` to subresources |
| `src/operator/images/dnsmasq/Containerfile` | Add `openssh-clients`, `sshpass` packages |

## Not in scope

- `vm_ready` endpoint changes (it hardcodes SSH — can be updated in a follow-up to try guest-agent first).
- KubeVirt serial as a distinct method (identical to console on KubeVirt).
- SSH key auth from dnsmasq pod (password only for now).

## Testing

1. Troshkad VM with guest agent running → uses guest-agent method
2. Troshkad VM without guest agent → falls back to SSH
3. KubeVirt VM with guest agent → uses guest-agent via virt-launcher pod
4. KubeVirt VM without guest agent → falls back to SSH via dnsmasq pod
5. Explicit `method=ssh` → uses SSH regardless of agent availability
6. Explicit `method=guest-agent` on VM without agent → returns 503 error
7. RHEL VMs (qemu-guest-agent installed by default) and RHCOS (via ignition)
