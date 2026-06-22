# IBI Standalone Redesign — Seed Build + Deploy

**Date**: 2026-06-22
**Status**: Draft
**Goal**: End-to-end standalone IBI (Image-Based Installation) for SNO on Troshka, using Redfish throughout — suitable as a datacenter demo.

## Background

IBI deploys a Single Node OpenShift cluster from a pre-captured seed image in ~15 minutes (vs ~45 min for agent installer). The seed contains the full OS, container images, and cluster state. A config ISO provides per-site details (hostname, network, certs). The `lca-cli` binary (from the Lifecycle Agent) handles seed restore and post-pivot reconfiguration.

Previous attempts stalled because:
- `/var/lib/containers` not mounting on the target VM's second disk (vdb)
- No debugging access to the post-restore RHCOS system (no root password, SSH keys come from config ISO)
- Incorrect assumption that sushy-emulator loses virtual media on power cycles (it doesn't — `defineXML()` persists the CDROM)

## Key Findings from Investigation

### Sushy-emulator CDROM handling is correct
- `set_boot_image()` calls `conn.defineXML()` — updates **persistent** domain config
- CDROM survives `virsh destroy`/`virsh start`
- No sushy patching required

### Boot order interaction is safe
- Troshka creates blank VMs with `<os><boot dev="hd"/></os>` (legacy style)
- Sushy's `set_boot_device()` is only called when current boot device matches media type — for a blank VM (boot=hd, media=cd), it's **not called**
- OVMF firmware falls through from blank HDD to CDROM automatically
- After seed restore, HDD has a bootloader → boots from disk, CDROM stays available as `/dev/sr0`

### Config ISO label is correct
- `openshift-install image-based create config-image` creates ISO with volume label `cluster-config` (constant `BlockDeviceLabel` in `pkg/types/imagebased/seedreconfiguration.go`)
- `lca-cli` post-pivot service scans for block device with this label via `lsblk`
- Alternative path: `/opt/openshift/cluster-configuration/` (filesystem fallback)

### Real blockers
1. **vdb mount**: Seed captures MachineConfig systemd units for formatting/mounting vdb. These may not survive seed restore, or may conflict with lca-cli's own container storage setup.
2. **No debugging access**: After seed restore + reboot, can't log into RHCOS to observe lca-cli behavior.

## Architecture

```
Seed Build (one-time):
  Troshka SNO (agent install) → cluster healthy → build-sno-seed.sh → registry

IBI Deploy (per-site, all Redfish):
  bastion: RHEL, generates ISOs, serves via HTTP, orchestrates via Redfish BMC
  cp-0: blank VM, UEFI, 2 disks (vda=200GB boot, vdb=250GB containers), BMC enabled

  Flow:
    1. openshift-install image-based create image     → installation ISO (rhcos-ibi.iso)
    2. openshift-install image-based create config-image → config ISO (imagebasedconfig.iso, label=cluster-config)
    3. Redfish InsertMedia (installation ISO) → ForceOn → boot from ISO
    4. lca-cli ibi: install RHCOS, restore seed, pre-cache images
    5. Seed restore complete (SSH check from bastion)
    6. Redfish EjectMedia (installation ISO)
    7. Redfish InsertMedia (config ISO) → ForceOff → ForceOn
    8. Boot from disk (RHCOS), lca-cli post-pivot finds cluster-config ISO
    9. recert runs → crio/kubelet start → cluster available
```

## Design

### Part 1: Seed Build Process

**Source SNO requirements:**
- Deployed via Troshka agent installer (or any method producing a healthy SNO)
- Two disks: vda (boot, 200GB+), vdb (250GB+, mounted at `/var/lib/containers`)
- `/var/lib/containers` on vdb via MachineConfig (the `build-sno-seed.sh` script handles this)
- No RHACM/MCE installed
- Pull-through registry configured to avoid quay.io throttling
- Cluster fully healthy (node Ready, 0 degraded/progressing operators)

**Seed capture:**
- Script: `scripts/build-sno-seed.sh` (already exists, 506 lines)
- Installs LCA operator via OLM subscription
- Creates registry auth secret from local podman auth
- Applies `SeedGenerator` CR → LCA captures ostree + container images → pushes OCI image to registry
- Cluster is destroyed during capture (expected, non-recoverable)
- Output: OCI image at specified tag (e.g., `quay.io/redhat-gpte/sno-seed:4.22`)

**Seed metadata** (captured automatically by LCA):
```json
{
  "seed_cluster_ocp_version": "4.22.0",
  "container_storage_mountpoint_target": "/dev/vdb",
  "recert_image_pull_spec": "registry.redhat.io/openshift4/recert-rhel9@sha256:..."
}
```

The `container_storage_mountpoint_target: /dev/vdb` is critical — the target VM must have a second disk at this path.

### Part 2: IBI Deploy — Ansible Role

**Role**: `host_ocp4_ibi_installer` (in agnosticd-v2)

**Phase 1 — Generate installation ISO:**
- Download `openshift-install` binary for target version
- Template `image-based-installation-config.yaml` with seed image, disk, network config, SSH key
- Run `openshift-install image-based create image`
- Serve ISO via HTTP on bastion

**Phase 2 — Boot from installation ISO:**
- Redfish `VirtualMedia.InsertMedia` with HTTP URL to installation ISO
- Redfish `ComputerSystem.Reset` (ForceOn if powered off, ForceRestart if on)
- Wait for seed restore via SSH check (`journalctl -u install-rhcos-and-restore-seed`)
- Expected time: 10-15 minutes

**Phase 3 — Generate and attach config ISO:**
- Template `install-config.yaml` (cluster name, base domain, pull secret, SSH key, network CIDRs)
- Template `image-based-config.yaml` (hostname, NTP sources, network config, release registry)
- Run `openshift-install image-based create config-image`
- Redfish `VirtualMedia.EjectMedia` (remove installation ISO)
- Redfish `ComputerSystem.Reset` ForceOff → wait 10s
- Redfish `VirtualMedia.InsertMedia` (config ISO)
- Redfish `ComputerSystem.Reset` ForceOn
- VM boots from disk (seed-restored RHCOS), lca-cli finds config ISO at `/dev/sr0`

**Phase 4 — Wait for cluster:**
- Poll `oc get clusterversion` from bastion until Available=True
- Copy kubeconfig to bastion user
- Eject config ISO via Redfish
- Stop HTTP server

### Part 3: Debugging Access

**Root password in config ISO ignition:**
Add ignition override to `image-based-config.yaml` that sets a temporary root password. This enables VNC console login for debugging the post-restore boot without modifying the seed or breaking the flow.

The `image-based-config.yaml` supports ignition overrides — add a `passwd` section with a hashed root password. This gets applied during lca-cli's reconfiguration phase.

If reconfiguration itself is failing (lca-cli can't find the config ISO), the ignition override won't help. In that case, add the root password to the **installation ISO's ignition** via `image-based-installation-config.yaml` — this runs during the live ISO phase and can be used to set a password that persists into the seed-restored system via a post-install hook.

**VNC console for early boot:**
Use the Troshka VNC console to observe boot output visually (kernel messages, systemd unit failures, lca-cli logs). This works even when the VM is stuck — no login required to read the screen. With a root password set, you can also interact from VNC.

**Serial exec for post-boot:**
Once the VM is up (even if the cluster isn't), serial exec can run commands:
```bash
./scripts/vm-exec.sh <project-id> <vm-id> "journalctl -b 0 --no-pager | tail -100"
```
This requires the VM to be running with a serial console (RHCOS enables it by default).

### Part 4: vdb / Container Storage Fix

**The problem:**
The seed SNO has `/var/lib/containers` on `/dev/vdb` via a MachineConfig with:
- `systemd-mkfs@dev-vdb.service` — formats vdb as XFS (no-op if already formatted)
- `var-lib-containers.mount` — mounts vdb at `/var/lib/containers`
- `restorecon-var-lib-containers.service` — fixes SELinux contexts

When LCA captures the seed, these systemd units are part of the ostree and should be included in the seed image. On the target VM, vdb is blank — `systemd-makefs` should detect the unformatted disk and create an XFS filesystem.

**Debugging approach:**
1. Run a clean deploy with debugging access enabled (root password)
2. After post-restore reboot, check from VNC console:
   - `systemctl status systemd-mkfs@dev-vdb.service`
   - `systemctl status var-lib-containers.mount`
   - `mount | grep containers`
   - `lsblk`
3. If the units don't exist → seed capture strips MachineConfig units → need to add vdb setup to config ISO ignition
4. If the units exist but fail → check logs for the specific error (device not found, ordering issue, etc.)

**Fallback fix:**
If MachineConfig units don't survive seed capture, add vdb formatting to the config ISO's ignition:
```json
{
  "systemd": {
    "units": [
      {
        "name": "format-vdb.service",
        "enabled": true,
        "contents": "[Unit]\nBefore=var-lib-containers.mount\nConditionPathExists=!/dev/disk/by-label/containers\n\n[Service]\nType=oneshot\nExecStart=/usr/sbin/mkfs.xfs -L containers /dev/vdb\n\n[Install]\nWantedBy=multi-user.target"
      }
    ]
  }
}
```

### Part 5: Pull-Through Registry

**Why**: quay.io throttles pulls. The pull-through registry at `registry-quay-quay-enterprise.apps.ocpv-infra01.dal12.infra.demo.redhat.com` mirrors `registry.redhat.io` and `quay.io`.

**Where it matters:**
1. **Seed build**: The source SNO should use the pull-through registry for initial image pulls (configured in agnosticv common.yaml)
2. **Installation ISO**: The `image-based-installation-config.yaml` references the seed image — if the seed is in quay.io, the live ISO pulls it during restore. Consider mirroring the seed image through the pull-through registry.
3. **Config ISO**: The `install-config.yaml` pull secret must include auth for the pull-through registry. If the target cluster needs to pull images post-install, `registries.conf.d` mirror config should be injected via the config ISO's ignition or as an extra manifest (ImageDigestMirrorSet).

**Implementation:**
- The agnosticv `common.yaml` already has `pull_through_registry` config with org mappings
- The ansible role should inject `registries.conf.d` mirror config via the config ISO when pull-through is enabled
- The `ImageDigestMirrorSet` manifest can be placed in the config ISO's `extra-manifests/` directory

### Part 6: Troshka Template

**Infra template** (`troshka/OCP4-SNO-IBI/infra_template.yaml`):
```yaml
template_name: ocp-sno-ibi
display_name: "Single Node OpenShift (IBI — Fast Deploy)"
description: "Standalone SNO via Image-Based Install (~15 min)"
category: openshift
install_method: ibi
deploy_time: "~15 min"

ocp:
  cluster_name: sno
  base_domain: sno.local
  api_vip: 10.0.0.10
  ingress_vip: 10.0.0.10

dns_records:
  - name: api.sno.sno.local
    target: cp-0
  - name: .apps.sno.sno.local
    target: cp-0

networks:
  cluster:
    cidr: 10.0.0.0/24
    dhcp: true
    gateway: true
    domain: sno.local
  bmc:
    cidr: 192.168.50.0/24
    type: bmc

vms:
  bastion:
    role: bastion
    vcpus: 2
    ram_gb: 4
    os: rhel9
    disks:
      - size_gb: 80
    nics:
      - network: cluster
        ip: 10.0.0.50
      - network: bmc
        ip: 192.168.50.50
  cp-0:
    role: blank
    vcpus: 16
    ram_gb: 32
    os: blank
    firmware: uefi
    power_on: false
    bmc: true
    bmc_ip: 192.168.50.10
    disks:
      - size_gb: 200
      - size_gb: 250
    nics:
      - network: cluster
        ip: 10.0.0.10
```

No changes needed to the Troshka template — it already has the correct layout (2 disks, UEFI, BMC, power_on=false).

## Implementation Phases

### Phase 1: Get it working (priority)
1. Add debugging access (root password in config ISO ignition)
2. Run clean end-to-end deploy with no manual VM intervention
3. Diagnose vdb mount issue from VNC console
4. Fix vdb issue (MachineConfig survival or ignition fallback)
5. Verify lca-cli post-pivot finds config ISO and completes reconfiguration
6. Document the working flow

### Phase 2: Catalog-ready
1. Error handling in ansible role (timeouts, retries, clear failure messages)
2. Pull-through registry integration (ImageDigestMirrorSet in config ISO)
3. Idempotency — role skips if cluster already installed
4. agnosticv catalog item polish (display name, description, parameters)

### Phase 3: Optimize
1. Pre-cache seed image on hosts (avoid pulling during deploy)
2. Parallel ISO generation (installation + config ISOs can be generated concurrently)
3. Measure and document actual deploy times

## Files Modified

**agnosticd-v2** (external repo):
- `ansible/roles/host_ocp4_ibi_installer/tasks/main.yml` — debugging access, flow fixes
- `ansible/roles/host_ocp4_ibi_installer/templates/image-based-config.yaml.j2` — ignition overrides for root password + vdb
- `ansible/roles/host_ocp4_ibi_installer/defaults/main.yml` — new defaults for debug password, pull-through config

**troshka**:
- `docs/ibi-status.md` — update with findings from this investigation
- `scripts/test-ibi-deploy.sh` — minor updates for pull-through registry creds

**agnosticv** (external repo):
- `troshka/OCP4-SNO-IBI/common.yaml` — pull-through registry config (already present)

## Non-Goals
- ACM/hub-based IBI (this is standalone only)
- Patching sushy-emulator (not needed — CDROM persistence works)
- Multi-node IBI (SNO only)
- Troshka backend code changes (all changes are in ansible role + templates)

## References
- [OpenShift IBI docs](https://docs.okd.io/latest/edge_computing/image_base_install/ibi_deploying_sno_clusters/ibi-edge-image-based-install-standalone.html)
- [Config ISO label source](https://github.com/openshift/installer/blob/main/pkg/types/imagebased/seedreconfiguration.go) — `BlockDeviceLabel = "cluster-config"`
- [Config ISO creation source](https://github.com/openshift/installer/blob/main/pkg/asset/imagebased/configimage/configiso.go) — `configImageLabel = imagebased.BlockDeviceLabel`
- [Installation script source](https://github.com/openshift/installer/blob/main/data/data/imagebased/files/usr/local/bin/install-rhcos-and-restore-seed.sh)
- [IBI tutorial](https://cloudcult.dev/openshift-image-based-installation-tutorial-part1-2/)
