# Plan: Guest Agent Exec + Unified Exec Priority

## Goal

Add `qemu-guest-agent` as the primary exec method across all providers, and unify the exec fallback order to: **guest-agent → SSH → console → serial**.

## Current State

The exec endpoint (`POST /projects/{id}/vms/{vm_id}/exec`) supports `method` parameter: `auto`, `ssh`, `serial`, `console`. All methods go through troshkad endpoints (`/vm/ssh-exec`, `/vm/serial-exec`, `/vm/console-exec`). KubeVirt native has no exec implementation at all.

## Auto Priority Order (all providers)

1. **Guest agent** — structured command execution via qemu-guest-agent. No network, no TTY parsing. Returns stdout, stderr, exit code cleanly.
2. **SSH** — SSH to VM IP. Requires network connectivity + credentials.
3. **Serial** — serial PTY / KubeVirt console subresource. TTY-based, pexpect-style.
4. **Console** — libvirt console (`virsh console`). Similar to serial but different libvirt path.

## Implementation

### 1. Troshkad: Add guest-agent exec (`/vm/guest-exec`)

New troshkad endpoint that runs:
```
virsh qemu-agent-command <domain> '{"execute":"guest-exec","arguments":{"path":"/bin/sh","arg":["-c","<command>"],"capture-output":true}}'
```
Then polls with `guest-exec-status` until the process completes. Returns base64-decoded stdout/stderr + exit code.

Prerequisites: `qemu-guest-agent` must be running in the VM. Check with `virsh qemu-agent-command <domain> '{"execute":"guest-info"}'` first — if it fails, the agent isn't available and we fall back.

### 2. KubeVirt Native: Guest agent exec via K8s API

KubeVirt exposes guest agent commands via:
```
PUT /apis/subresources.kubevirt.io/v1/namespaces/{ns}/virtualmachineinstances/{name}/guestosinfo
```
And exec via the guest agent may need `virtctl` or direct API calls. Research the exact KubeVirt API for guest-exec.

Alternative: if KubeVirt doesn't expose guest-exec as a subresource, we can use the `exec` subresource on the virt-launcher pod to run `virsh qemu-agent-command` inside it (the virt-launcher pod has access to the libvirt socket).

### 3. KubeVirt Native: SSH exec

The backend can't reach VM IPs directly (OVN secondary network). Options:
- **Exec into dnsmasq pod** — run SSH from inside the dnsmasq pod (it's on the same network). Use `kubernetes.stream.stream()` to exec into the pod and run `ssh`.
- **Dedicated exec pod** — lightweight pod on the secondary network with SSH client.

### 4. KubeVirt Native: Console/Serial exec

Use KubeVirt console subresource:
```
/apis/subresources.kubevirt.io/v1/namespaces/{ns}/virtualmachineinstances/{name}/console
```
WebSocket-based serial console. Implement pexpect-style send/receive over WebSocket. Same prompt-parsing challenges as troshkad's serial exec.

### 5. Backend: Unified exec routing

In `projects.py` `vm_exec` endpoint:
- For troshkad hosts: call `/vm/guest-exec`, fall back to `/vm/ssh-exec`, then `/vm/serial-exec`, then `/vm/console-exec`
- For kubevirt-cluster hosts: try guest-agent API, fall back to SSH-via-pod, then serial (console subresource), then console
- `method` parameter: `auto` (default, tries all in order), `guest-agent`, `ssh`, `serial`, `console`

### 6. RBAC

Add to `infra/ocpvirt-rbac.yaml`:
```yaml
- apiGroups: ["subresources.kubevirt.io"]
  resources: ["virtualmachineinstances/console", "virtualmachineinstances/exec"]
  verbs: ["get"]
```

## Files to modify

### Troshkad
- `src/troshkad/troshkad.py` — add `/vm/guest-exec` endpoint

### Backend
- `src/backend/app/api/projects.py` — add kubevirt exec branch, unified auto fallback
- `src/backend/app/services/troshkad_client.py` — add guest-exec client method (if needed)

### Operator
- `infra/ocpvirt-rbac.yaml` — add console/exec subresource permissions

## Testing

1. Exec on a troshkad-hosted VM with guest agent running — should use guest-agent
2. Exec on a troshkad-hosted VM without guest agent — should fall back to SSH
3. Exec on a KubeVirt native VM — should use guest-agent via KubeVirt API
4. Exec with `method=ssh` explicitly — should use SSH regardless of agent availability
5. Test on RHEL (has qemu-guest-agent by default) and RHCOS (has it via ignition)
