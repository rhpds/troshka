# Network Automation Workshop template

Deployable Troshka template: `net-automation-workshop.yaml`

Bootstrap, router day-0 config, showroom overlays, and pattern-build workflow
live in **demo_workloads**:

- Playbook: `~/demo_workloads/playbooks/net-automation-workshop/main.yml`
- Role: `~/demo_workloads/roles/troshka_workload_net_automation_workshop/`
- Docs: `~/demo_workloads/playbooks/net-automation-workshop/PATTERN-BUILD.md`

## Building bootstrap ISOs

Source configs live in `example_templates/net-automation-workshop/bootstrap/`.

**Local dev** (uploads to the Troshka instance library API):

```bash
./scripts/build-router-bootstrap-isos.sh --upload
```

**KubeVirt / shared clusters** need the ISOs in **central S4** (`s3_readonly` bucket)
so deploy can import CD-ROM PVCs. Upload with write credentials (not the readonly
provider key — that is read-only in the UI):

```bash
export CENTRAL_S4_BUCKET=troshka-gold-images
export CENTRAL_S4_ENDPOINT=https://<central-s4-host>   # if not AWS
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...

./scripts/build-router-bootstrap-isos.sh --upload-central --sync-central
```

`--sync-central` calls `POST /api/v1/library/sync-central` (admin) so library items
appear with `source=central`. You can also sync from **Admin → Library** after upload.

Library item names (referenced from `net-automation-workshop.yaml`):

- `net-automation-rtr1-bootstrap` — `iosxe_config.txt` (Cisco CVAC)
- `net-automation-rtr3-bootstrap` — `config/juniper.conf` (Junos KVM bootstrap)

ISOs are attached as SATA CD-ROM but **not** in boot order (`bootableIso: false`).
VMs still boot from disk; the NOS reads config from the ISO during boot.

### KubeVirt disk bus notes

| Router | Disk bus | Why |
|--------|----------|-----|
| rtr2, rtr4 (vEOS) | `sata` | virtio hangs after `kexec_core: Starting new kernel` on KubeVirt |
| rtr3 (vSRX) | `sata` | matches Junos KVM expectations on KubeVirt |
| rtr1 (IOS-XE) | virtio (default) | boots from disk + bootstrap ISO |

On libvirt (ocpvirt) virtio works for vEOS; SATA is safe on both providers.

## Bootstrap notes

After deploy, run the Ansible bootstrap from `demo_workloads` (not from this
repo). Key gotchas discovered in practice:

1. **Inventory filename** — the `troshka.cloud.troshka` plugin only loads
   `*.troshka.yml` files. Use
   `.generated/${TROSHKA_PROJECT_ID}/inventory.troshka.yml`, not
   `troshka_inventory.yml`.
2. **RHSM** — required for vscode package install on unregistered `rhel-9.6`
   images. Portal creds (`redhat_username`/`redhat_password`) in
   `~/agnosticv/includes/secrets/aap2-casc-registry-creds.yaml` — not satellite.
   Original zt-ansiblebu CI applies demosat content views at provision time instead.
3. **Showroom networking** — showroom listens on the transit infra IP;
   gateway 80/443→infra port-forwards are injected at deploy.
4. **Router day-0 bootstrap** — vrnetlab (container lab) and Troshka use
   different mechanisms per platform (ISO vs serial). See
   [`docs/dev/network-router-bootstrap.md`](../docs/dev/network-router-bootstrap.md).
   rtr1/rtr3 use bootstrap ISOs; rtr2/rtr4 use serial (`configure_routers`).
   On KubeVirt, vEOS VMs need **SATA** disk bus (template sets `bus: sata`).
