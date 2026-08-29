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

| Router | NICs | Disk bus | NIC model | Guest interfaces |
|--------|------|----------|-----------|------------------|
| rtr1 (IOS-XE) | 3 | virtio (default) | virtio | Gi1 (lab), Gi2/Gi3 (link nets) |
| rtr2, rtr4 (vEOS) | 3 / 2 | `sata` | **e1000** | Management1 (lab), Ethernet1/2 (link nets) |
| rtr3 (vSRX) | 2 | `sata` | virtio | fxp0 (lab), ge-0/0/0 (link net) |

Mirrors containerlab vrnetlab: adapter count/model order match QEMU; `lab` = mgmt
(172.20.20.x); `link-r1-r2`, `link-r1-r3`, `link-r2-r4` = dataplane /30 segments
(rtr1 Gi2↔rtr2 Eth1, rtr1 Gi3↔rtr3 ge-0/0/0, rtr2 Eth2↔rtr4 Eth1). Dataplane
NICs have no template IP — configure in lab exercises if needed.

On libvirt (ocpvirt) virtio works for vEOS disk; SATA is safe on both providers.

## Bootstrap notes

After deploy, run the Ansible bootstrap from `demo_workloads` (not from this
repo). Key gotchas discovered in practice:

1. **Inventory filename** — the `troshka.cloud.troshka` plugin only loads
   `*.troshka.yml` files. Use
   `.generated/${TROSHKA_PROJECT_ID}/inventory.troshka.yml`, not
   `troshka_inventory.yml`.
2. **RHSM** — required for vscode package install on unregistered `rhel-9.6`
   images. Demosat activation key in
   `~/zt-ansiblebu-agnosticv/includes/secrets/demosat-rhel-8-and-9-latest.yaml`
   (`satellite_org`, `satellite_activationkey` via `vault_var` — not portal
   creds). The secret sets `set_repositories_satellite_ha: true`; registration
   must use the Satellite HA `subscription-manager` path (`server.hostname` +
   `/rhsm` prefix, `--serverurl` / `--baseurl`). See
   `demo_workloads/playbooks/net-automation-workshop/PATTERN-BUILD.md`.
   Original zt-ansiblebu CI applies demosat content views at provision time instead.
3. **Showroom networking** — showroom listens on the transit infra IP;
   gateway 80/443→infra port-forwards are injected at deploy.
4. **Router day-0 bootstrap** — vrnetlab (container lab) and Troshka use
   different mechanisms per platform (ISO vs serial). See
   [`docs/dev/network-router-bootstrap.md`](../docs/dev/network-router-bootstrap.md).
   rtr1/rtr3 use bootstrap ISOs; rtr2/rtr4 use serial (`configure_routers`).
   On KubeVirt, vEOS VMs need **SATA** disk bus (template sets `bus: sata`) and
   **`legacy_root_bus: true`** so NICs land on the q35 root PCI bus (slots 03+).
