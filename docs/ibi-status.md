# IBI (Image-Based Install) Status — 2026-06-22

## What Works
- Seed image at `quay.io/redhat-gpte/sno-seed:4.22` (1.57GB, valid)
- `lca-cli ibi` extracted from seed, runs successfully during installation ISO boot
- Seed restore to disk: RHCOS install + ostree deploy + container image pre-cache
- Config ISO generation via `openshift-install image-based create config-image`
- Config ISO detection by `lca-cli` post-pivot service (scans for `cluster-config` label via `lsblk`)
- Ignition reads config ISO: "user-provided config was applied" confirmed
- ForceOff → insert config ISO → ForceOn flow works via Redfish BMC

## What Doesn't Work Yet
- **Cluster never comes up after config ISO applied** — crio and kubelet stay `inactive (dead)`
- Root cause: `lca-cli` post-pivot service loops "waiting for block device with label cluster-config" because **every time we `virsh destroy`/`virsh start` to debug, the sushy virtual media CDROM is lost**
- The `lca-cli` reconfiguration never completes → never starts crio/kubelet → no cluster

## Key Findings

### The IBI Flow (from source code)
1. Installation ISO boot → ignition runs `install-rhcos-and-restore-seed.service`
2. Service script (`install-rhcos-and-restore-seed.sh`) does:
   - `podman create` from seed image, `podman cp lca-cli` binary to host
   - Runs `lca-cli ibi -f /var/tmp/ibi-configuration.json`
3. `lca-cli ibi` does: clean disk → coreos-installer → create extra partition → pull seed → restore ostree → pre-cache images
4. VM reboots (logs "Skipping shutdown" — stays running, doesn't shut off)
5. On reboot, `lca-cli` post-pivot service starts:
   - Scans for block device with label `cluster-config` (the config ISO)
   - OR looks for config folder at `/opt/openshift/cluster-configuration/`
   - When found: runs `recert` (certificate regeneration), applies cluster config, starts crio/kubelet

Source: [openshift/installer](https://github.com/openshift/installer/blob/main/data/data/imagebased/files/usr/local/bin/install-rhcos-and-restore-seed.sh)

### Bugs Fixed
1. **`extraPartitionStart: -240G` on 200GB disk** → `sgdisk` crash. Fixed: disabled extra partition (lca-cli creates its own vda5)
2. **Config ISO filename** was `imagebasedconfig.iso` not `rhcos-ibi-config.iso`
3. **ForceRestart on shut-off VM** → no boot. Fixed: check PowerState, use ForceOn
4. **ForceOff before config ISO** — required so config ISO is present at boot time
5. **Overlay disk smaller than backing** → truncated GPT, emergency mode. Fixed: auto-expand
6. **Second disk (`/dev/vdb`) conflict** — seed expects vdb for /var/lib/containers, lca-cli cached to vda5. Solution: keep second disk (seed needs it), disable extraPartitionStart
7. **Missing `/dev/vdb`** → boot hangs "A start job is running for /dev/vdb"

### Architecture
```
Template: OCP4-SNO-IBI/infra_template.yaml
  bastion: RHEL 10, 4 vCPU, 8GB, 80GB disk + DVD ISO
  cp-0: blank, 16 vCPU, 32GB, 200GB boot + 250GB vdb, UEFI, BMC

Roles: ~/agnosticd-v2/ansible/roles/host_ocp4_ibi_installer/
  tasks/main.yml — full IBI flow
  tasks/boot_via_bmc.yml — Redfish virtual media boot
  tasks/eject_iso.yml — Redfish media eject
  templates/image-based-installation-config.yaml.j2
  templates/image-based-config.yaml.j2
  templates/install-config.yaml.j2
  defaults/main.yml — seed image, versions, timeouts

Catalog: ~/agnosticv/troshka/OCP4-SNO-IBI/
  common.yaml — deploy vars, seed image ref
  infra_template.yaml — VM topology

Test: ~/troshka/scripts/test-ibi-deploy.sh
```

### Config
```yaml
host_ocp4_ibi_seed_image: "quay.io/redhat-gpte/sno-seed:4.22"
host_ocp4_ibi_seed_version: "4.22.0"
host_ocp4_installer_version: "4.22"
host_ocp4_ibi_extra_partition_start: ""  # disabled, lca-cli creates vda5 itself
host_ocp4_ibi_installation_disk: /dev/vda
```

### Seed Image Metadata
```json
{
  "seed_cluster_ocp_version": "4.22.0",
  "base_domain": "ocp.local",
  "cluster_name": "ocp",
  "sno_hostname": "cp-0",
  "container_storage_mountpoint_target": "/dev/vdb",
  "recert_image_pull_spec": "registry.redhat.io/openshift4/recert-rhel9@sha256:...",
  "machine_networks": ["10.0.0.0/24"]
}
```

## Next Steps
1. **Do a clean deploy with NO manual VM intervention** — let the full flow run: seed restore → ForceOff → config ISO attach → ForceOn → lca-cli finds cluster-config → recert → crio/kubelet start
2. **DO NOT `virsh destroy`/`virsh start` to set passwords** — this drops the sushy virtual media CDROM and lca-cli loops forever
3. If the cluster still doesn't come up, check from the VNC console WITHOUT restarting the VM:
   - `journalctl -b 0 | grep lca-cli | tail -30`
   - `blkid | grep cluster-config`
   - `systemctl is-active crio kubelet`
4. The SSH key (`~/.ssh/ibi_key` on bastion) works during seed restore phase but NOT after reconfiguration (config ISO changes the authorized keys)
5. Consider adding root password to the config ISO's ignition for debugging

## Recert Integration (Roadmap)
`recert` can rename any SNO cluster post-deploy. Could be used with Troshka patterns:
- Pattern restore (2 min) + recert (3-5 min) = unique SNO in ~7 min
- Source: github.com/openshift/recert (Rust binary)
- Container: registry.redhat.io/openshift4/recert-rhel9
- SNO only (single etcd member requirement)
