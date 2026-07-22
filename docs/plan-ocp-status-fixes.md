# Plan: OCP Status Fixes

## Issue 1: OCP status never resets on redeploy

On redeploy, the old `ocp_status` ("ready") and `ocp_install_elapsed` values persist on the project. The UI shows stale "cluster ready (5m 29s)" from the previous deploy while the new deploy is still running.

The health monitor also never restarts because `maybe_start_ocp_health_monitor()` has two guards that block it:
1. `project.ocp_status != "monitoring"` — still "ready" from previous deploy (line 3610)
2. `project.ocp_install_elapsed is not None` — still set from previous deploy (line 3620)

**Root cause:** `redeploy_project()` (projects.py:3098) sets `project.state = "deploying"` but never clears `ocp_status` or `ocp_install_elapsed`.

**Fix:** Clear OCP status fields in the redeploy endpoint:

```python
# In redeploy_project(), after project.state = "deploying"
project.ocp_status = None
project.ocp_install_elapsed = None
```

**Files:**
- `src/backend/app/api/projects.py` — `redeploy_project()` (~line 3098)

---

## Issue 2: KubeVirt native has no OCP status monitoring

KubeVirt native projects have no troshkad agent, so the entire OCP health monitor pipeline never runs.

### Sub-issues discovered and fixed

**2a) DNS records missing from dnsmasq (FIXED)**

The operator's dnsmasq config generator didn't include `dnsRecords` from the topology. OCP DNS entries like `api.ocp.ocp.local` were never served by dnsmasq, breaking both OCP itself and any exec-based health monitoring.

Fixed in:
- `src/operator/helpers/topology.py` — `extract_networks()` now includes `dnsRecords`
- `src/operator/handlers/project.py` — passes `dnsRecords` to TroshkaNetwork CR spec
- `src/operator/helpers/dnsmasq.py` — generates `address=` lines from `dnsRecords`
- `src/operator/crds/troshkanetwork.yaml` — added `dnsRecords` to CRD schema
- `src/operator/handlers/network.py` — added `on.update` handler to reconcile ConfigMap + restart dnsmasq pod when spec changes

**2b) Guest-agent exec won't work for OCP health monitoring (ABANDONED)**

Testing showed `virt_qemu_ga_t` SELinux domain is too confined:
- Can't execute Go binaries (`oc`) — needs `execmem` for Go runtime
- Can't read `user_home_t` files (kubeconfig) — needs file policy
- Can't open network sockets — needs `tcp_socket` permissions
- Can't switch users (`su`, `sudo`, `runuser` all blocked)

Each layer of SELinux policy we added exposed the next blocker. The `virt_qemu_ga_t` domain was designed to be confined — fighting it is the wrong approach.

**2c) SSH exec label mismatch (found, not yet fixed)**

`kubevirt_exec_ssh()` searches for pods with `app=dnsmasq,troshka-project={project_id[:8]}` but dnsmasq pods are labeled `app=troshka-dnsmasq,troshka-network=net-{id}`. SSH exec silently fails with "No running dnsmasq pod found."

### Proposed fix: dedicated exec pod

Instead of guest-agent or fixing SSH exec through dnsmasq, create a purpose-built exec pod per KubeVirt project:

- **Image**: `troshka-tools` (already exists) or new `troshka-exec` — needs `oc`, `sshpass`, `ssh`, `curl`
- **Network**: attached to the cluster network NAD (same as dnsmasq) — can reach VMs at their IPs
- **Lifecycle**: created during project deploy, deleted during destroy
- **RBAC**: no special permissions needed — it's just a network-attached pod running commands
- **Usage**: the OCP health monitor calls `kubectl exec` into this pod to run `oc get co`, `oc get nodes`, etc.
- **Benefits**:
  - No SELinux constraints (normal container)
  - Runs as non-root user (`cloud-user` equivalent)
  - Has full network access to project VMs
  - Replaces the broken `kubevirt_exec_ssh` dnsmasq path
  - One pod per project, not per network (cleaner than reusing dnsmasq)

**Pod spec sketch:**
```yaml
name: exec-{project_id[:8]}
labels:
  app: troshka-exec
  troshka-project: {project_id[:8]}
annotations:
  k8s.v1.cni.cncf.io/networks: {cluster_network_nad}
spec:
  containers:
  - name: exec
    image: quay.io/redhat-gpte/troshka-tools:latest
    command: ["sleep", "infinity"]
```

The health monitor would `kubectl exec` into this pod and run `oc` commands with the kubeconfig copied from the bastion (or mounted from a Secret).

**Open question:** How does the exec pod get the kubeconfig? Options:
- A) Copy from bastion via `sshpass`/`ssh` at health monitor start time
- B) Mount the bastion's kubeconfig as a Secret (created during deploy)
- C) SSH to bastion from the exec pod (same as current troshkad path, just different pod)

Option C is simplest — the exec pod is just a better version of the dnsmasq pod for SSH exec. The health monitor SSHes into the bastion from the exec pod, runs `oc` commands there. No kubeconfig transfer needed.

**Files:**
- `src/operator/helpers/k8s.py` — `build_exec_pod()` function
- `src/operator/handlers/project.py` — create exec pod during deploy, delete during destroy
- `src/backend/app/services/providers/kubevirt.py` — `kubevirt_exec_ssh()` uses exec pod instead of dnsmasq pod
- `src/backend/app/services/deploy_service.py` — `maybe_start_ocp_health_monitor()` relaxes guard for kubevirt-cluster, `_exec_on_bastion()` routes through exec pod
