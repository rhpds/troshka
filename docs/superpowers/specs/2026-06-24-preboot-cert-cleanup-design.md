# Pre-Boot Kubelet Cert Cleanup for Pattern Deploys

## Problem

When an OCP cluster is captured as a pattern and later deployed, the kubelet certificates baked into the disk image may have expired (kubelet certs rotate every 24 hours). If kubelet starts with expired certs, it enters a retry/backoff loop before requesting new ones — adding 10+ minutes to cluster boot time. Same-day deploys from a pattern boot in ~6 minutes; next-day deploys take 15+ minutes.

## Solution

Delete stale kubelet PKI from RHCOS node disks **offline** (before VM startup) so kubelet immediately bootstraps fresh certificates on first boot — the same fast path as a fresh install.

**Files deleted:**
- `/var/lib/kubelet/pki` (directory — client and server certs/keys)
- `/var/lib/kubelet/kubeconfig` (file — embeds client certificate)

**Not touched:** `/etc/kubernetes/` — the existing kube-apiserver force-redeployment patch (deploy_service.py:3131) handles that side.

**Works in concert with existing CSR approver** — deleting stale certs means kubelet submits fresh CSRs immediately on boot instead of retrying expired certs. The existing `_approve_pending_csrs()` polling (Phases 2-5 of OCP health monitor) approves them.

## Scope

- **Pattern deploys only** — fresh installs generate certs during install, no cleanup needed
- **OCP topologies only** — detected by `_is_ocp_topology()` (bastion + rhcos nodes)
- **RHCOS nodes only** — bastion (os: rhel) has no kubelet
- **SNO and multi-node** — SNO has 1 RHCOS node, multi-node has 3-5+

## Architecture

### 1. New troshkad endpoint: `POST /vms/modify-fs`

Generic offline filesystem modification via guestfish. Accepts a disk path and a list of allowlisted operations.

**Parameters:**
```json
{
  "disk": "/var/lib/troshka/vms/{project_id}/{vm[:8]}-{disk[:8]}.qcow2",
  "operations": [
    {"action": "rm-rf", "path": "/var/lib/kubelet/pki"},
    {"action": "rm-f", "path": "/var/lib/kubelet/kubeconfig"}
  ]
}
```

**Supported operations (allowlisted):**
- `rm-rf` — remove directory recursively
- `rm-f` — remove file (no error if missing)
- `mkdir-p` — create directory with parents
- `write` — write content to file (`content` field)
- `upload` — upload local file to guest (`local_path` → `path`)
- `chmod` — change permissions (`mode` + `path`)

**Guestfish invocation:**
```bash
guestfish --rw -a /path/to/disk.qcow2 -i <<EOF
rm-rf /var/lib/kubelet/pki
rm-f /var/lib/kubelet/kubeconfig
EOF
```

The `-i` flag auto-inspects the disk and mounts filesystems at correct mount points. For RHCOS, this handles the ostree partition layout and mounts `/var` correctly without manual partition discovery.

**Error handling:** Each operation runs independently. Failures are logged but don't stop subsequent operations. Returns per-operation success/failure summary.

**Locking:** Runs between VM define (`virt-install --noreboot`) and VM start (`virsh start`). The disk exists on the filesystem but no QEMU process has it open. Guestfish acquires its own exclusive lock.

### 2. Agent installer: add `libguestfs-tools`

Add `libguestfs-tools` to the `dnf install` package list in `agent_deployer.py`. ~100MB, provides guestfish and supporting libraries.

### 3. Deploy pipeline: new step between define and start

Insertion point in `deploy_service.py`:

```
Step 4:  Create VM disks and definitions (existing)
Step 4d: Clean kubelet certs on RHCOS disks (NEW)
Step 5:  Start VMs (existing)
```

**Logic:**
1. Check `_is_pattern_deploy(topology) and _is_ocp_topology(topology)` — skip if either is false
2. Collect boot disk path for each VM where `os == "rhcos"` — boot disk is the first qcow2 storage node returned by `_find_vm_disks()` (the system disk containing `/var`)
3. Call `POST /vms/modify-fs` on each disk sequentially (guestfish is I/O heavy)
4. Log results, proceed to VM start regardless of success/failure (non-fatal)
5. Update deploy progress UI: "cleaning kubelet certificates"

**Timing:** 2-5 seconds per disk. SNO = 1 disk, standard 3+2 = 5 disks, compact = 3 disks.

## Why This Works

From the [official Red Hat disaster recovery docs](https://docs.okd.io/latest/backup_and_restore/control_plane_backup_and_restore/disaster_recovery/scenario-3-expired-certs.html):

> Recover kubelet on each node: stop kubelet, `rm -rf /var/lib/kubelet/pki /var/lib/kubelet/kubeconfig`, restart kubelet.

When kubelet starts with no certs, it immediately generates a new key and submits a bootstrap CSR. When it starts with expired certs, it tries them first, gets TLS handshake failures, backs off, and only then falls into the CSR path. That retry/backoff is where the 10+ minute penalty comes from.

By doing the deletion offline before boot, we skip the retry loop entirely.

## Testing

**Unit test:** Verify deploy pipeline filtering — RHCOS VM identification, boot disk selection, and that the troshkad call only fires for pattern + OCP topologies. Mock the troshkad call.

**Manual validation:**
1. Deploy from an existing pattern that is >24 hours old (no clock_target)
2. Confirm progress UI shows cert cleanup step
3. Verify boot time is ~6 min instead of ~15 min
4. Check `oc get csr` — fresh CSRs approved quickly, no expired cert errors in kubelet logs
