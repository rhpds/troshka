# libvirt-host-image

A [bootc](https://containers.github.io/bootc/) image definition for a disposable
Fedora VM that Troshka's `libvirt` provider type adopts as a host — useful for
running Troshka fully locally (no cloud account needed) by nesting the
lab VMs it creates inside one VM on your own machine.

Unlike the other providers (`ec2`, `gcp`, `azure`, `ocpvirt`), `libvirt` doesn't
call a cloud API to create a host — it just needs an existing SSH-reachable
Linux box with libvirt + nested virtualization. This image is one convenient
way to build that box: define it as a `Containerfile`, build/push it to
quay.io like a normal container image, then convert it to a qcow2 and boot it
with `virt-install --import`. Rebuilding later is just editing the
`Containerfile` and re-running two commands — no Anaconda install screens.

The image itself carries **no SSH key material** — it's a generic, shareable
base OS meant to live in the org's `quay.io/rhpds` namespace and eventually be
built by CI. Each person injects their *own* public key at `virt-install`
time via `--cloud-init` (step 4), never baking a personal credential into
anything that gets pushed to a registry.

## 0. Prerequisites on your libvirt host machine

Confirm nested virtualization is available:

```bash
cat /sys/module/kvm_intel/parameters/nested   # Intel: should print Y
cat /sys/module/kvm_amd/parameters/nested     # AMD: should print 1
```

If Intel prints `N`, enable it once:

```bash
echo "options kvm_intel nested=1" | sudo tee /etc/modprobe.d/kvm-nested.conf
sudo modprobe -r kvm_intel && sudo modprobe kvm_intel
```

Make sure the tooling is installed and running:

```bash
sudo dnf install -y libvirt qemu-kvm virt-install
sudo systemctl enable --now libvirtd
sudo usermod -aG libvirt "$USER"   # re-login after this (new terminal tab isn't enough)
```

> **Running `virt-install`/`virsh` without `sudo`:** group membership alone
> isn't enough — libvirt defaults non-root clients to the unprivileged
> `qemu:///session` instance, which can't see `/var/lib/libvirt/images` or
> anything defined under `qemu:///system`. Either keep `sudo` in front of
> every command in this guide, or set `export LIBVIRT_DEFAULT_URI=qemu:///system`
> once (Fedora's polkit rules make this passwordless for `libvirt`-group
> members) — just don't mix the two approaches.

## 1. Generate the SSH keypair

```bash
ssh-keygen -t ed25519 -f ~/.ssh/troshka-libvirt -C troshka-libvirt-host -N ""
```

The private half (`cat ~/.ssh/troshka-libvirt`) is the value that goes into
the Troshka provider's `ssh_private_key` credential in step 6. The public
half is only ever used locally, in step 4 — it never gets baked into the
image or pushed anywhere.

## 2. Build and push the image

```bash
podman login quay.io

podman build \
  -t quay.io/rhpds/troshka-libvirt-host:latest \
  -f infra/libvirt-host-image/Containerfile .

podman push quay.io/rhpds/troshka-libvirt-host:latest
```

Since the image carries no personal credentials, anyone on the team can
build and push it (or a future CI job can), and no rebuild is ever needed
just because someone's key changed.

> A GitHub Actions workflow to build/push this image automatically is
> planned but not wired up yet — for now, build and push it locally as
> above. Once added, it should target the same `quay.io/rhpds/troshka-libvirt-host`
> repo.

## 3. Convert it to a qcow2 with image-builder

`bootc-image-builder` is being deprecated in favor of the unified
[`image-builder`](https://github.com/osbuild/image-builder-cli) CLI/container
(same osbuild project — see the
[deprecation notice](https://osbuild.org/docs/bootc/deprecation-notice/)).
Unlike `bootc-image-builder`, which only ever shipped opaque commit-hash
tags, `image-builder-cli` has real semver releases, so we pin to one instead
of `:latest`.

```bash
sudo podman pull quay.io/rhpds/troshka-libvirt-host:latest
mkdir -p output
sudo podman run --rm --privileged --pull=newer \
  --security-opt label=type:unconfined_t \
  -v ./output:/output \
  -v /var/lib/containers/storage:/var/lib/containers/storage \
  ghcr.io/osbuild/image-builder-cli:v78.0.0 \
  build --output-dir /output \
  --bootc-ref quay.io/rhpds/troshka-libvirt-host:latest \
  --bootc-default-fs ext4 \
  qcow2
```

> `--bootc-default-fs ext4` is required for Fedora bootc images specifically —
> unlike RHEL/CentOS Stream, Fedora's don't embed a default root filesystem,
> so `image-builder` fails with `missing required info: DefaultRootFs`
> without it ([upstream note](https://github.com/osbuild/bootc-image-builder#-quickstart)).

> **Referencing a locally-built image** (before you've pushed to quay.io):
> spell out the full `localhost/` prefix, e.g. `--bootc-ref
> localhost/troshka-libvirt-host:dev`, even though plain `podman run
> troshka-libvirt-host:dev` would resolve fine without it —
> `image-builder-cli` treats unqualified refs as `docker.io` pulls, not local
> lookups, and fails with `does not resolve to an image ID`. Double-check the
> exact tag with `podman images` first, since a typo'd prefix (e.g.
> `locahost/...`) won't be caught early — `podman build -t`/`push` accept it
> silently as its own local tag.

This drops a `.qcow2` file directly into `output/` (the exact basename is
printed at the end of the build log — capture it for the next step):

```bash
QCOW2=$(ls output/*.qcow2 | head -1)
```

## 4. Boot it with virt-install, plus the required second disk

Troshka's agent installer hard-requires a **second, unformatted data disk**
(`/dev/vdb`) and will `exit 1` without one — that's a libvirt VM-definition
detail independent of the OS image, so it's attached here rather than baked
into the `Containerfile`.

This is also where your personal SSH key gets injected — via a small
NoCloud `user-data` file (below), attached as a CDROM for first boot only.
`cloud-init` (pre-installed by the `Containerfile`) adds the key to
`/root/.ssh/authorized_keys`, and the `runcmd` disables cloud-init afterward
so it never touches auth again.

We write our own `user-data` instead of using virt-install's
`--cloud-init root-ssh-key=...,disable=on` shorthand so we can add
`preserve_hostname: true`, which that shorthand doesn't expose. Without it,
cloud-init's `set_hostname` module calls `hostnamectl` on every first boot
and reliably loses a race with D-Bus on composefs/ostree images — harmless
(it's just re-asserting the existing hostname), but shows up as 5 "failed"
systemd units on first login. `preserve_hostname: true` skips that module,
leaving the hostname the `Containerfile` already set in `/etc/hostname` —
which needs no D-Bus at all.

```bash
sudo cp "$QCOW2" /var/lib/libvirt/images/troshka-host.qcow2
sudo qemu-img create -f qcow2 /var/lib/libvirt/images/troshka-data.qcow2 50G
# Named explicitly, not via a troshka-*.qcow2 glob: /var/lib/libvirt/images is
# root:root mode 0711, so *your* shell (not the sudo'd command) can't list it
# to expand a wildcard — zsh errors "no matches found" despite the files
# existing.
sudo chown qemu:qemu /var/lib/libvirt/images/troshka-host.qcow2 \
  /var/lib/libvirt/images/troshka-data.qcow2
sudo restorecon -Rv /var/lib/libvirt/images/ 2>/dev/null || true

cat > /tmp/troshka-libvirt-user-data.yaml <<EOF
#cloud-config
preserve_hostname: true
users:
  - default
  - name: root
    ssh_authorized_keys:
      - $(cat ~/.ssh/troshka-libvirt.pub)
runcmd:
  - echo "Disabled by virt-install" > /etc/cloud/cloud-init.disabled
EOF

sudo virt-install \
  --name troshka-libvirt-host \
  --memory 12288 --vcpus 6 \
  --cpu host-passthrough \
  --disk /var/lib/libvirt/images/troshka-host.qcow2,bus=virtio \
  --disk /var/lib/libvirt/images/troshka-data.qcow2,bus=virtio \
  --cloud-init user-data=/tmp/troshka-libvirt-user-data.yaml \
  --network network=default \
  --os-variant generic \
  --import \
  --noautoconsole
```

`--cpu host-passthrough` passes VMX/SVM through to the guest so it can run
nested VMs. The two `--disk` flags map to `/dev/vda` (OS) and `/dev/vdb`
(empty data disk) inside the guest, in that order — the cloud-init seed ISO
is a separate CDROM device and doesn't disturb that ordering.

## 5. Find the VM's IP and verify

```bash
virsh domifaddr troshka-libvirt-host
ssh -i ~/.ssh/troshka-libvirt root@<vm-ip>
```

If the connection is refused, cloud-init may still be finishing its first-boot
run (usually seconds, not minutes) — retry after a moment.

Your first login should be clean (no `[systemd] Failed Units` banner — the
`preserve_hostname: true` above heads off the one D-Bus race that used to
cause it). If you do see one, `cloud-init status --long` shows which module
failed.

Inside the VM:

```bash
lsblk                          # vda (OS) + vdb (no filesystem) — leave vdb alone
egrep 'vmx|svm' /proc/cpuinfo  # confirms nested virt passthrough worked
sudo virt-host-validate        # fuller virt-stack check — all lines should PASS
```

If `firewalld` is active and the agent has trouble connecting later on port
31337:

```bash
sudo firewall-cmd --permanent --add-port=31337/tcp
sudo firewall-cmd --reload
```

## 6. Register it with Troshka

```bash
curl -X POST http://localhost:8200/api/v1/providers \
  -H "Authorization: Bearer $TROSHKA_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"local-libvirt","type":"libvirt","credentials":{"ssh_private_key":"<paste PEM key>"}}'

curl -X POST http://localhost:8200/api/v1/hosts \
  -H "Authorization: Bearer $TROSHKA_TOKEN" -H "Content-Type: application/json" \
  -d '{"provider_id":"<id-from-previous-call>","ip_address":"192.168.x.x","instance_type":"manual","disk_gb":100}'
```

Then poll `GET /api/v1/hosts` until `agent_status` is `"connected"` and
deploy a project as usual — placement will pick this host once it has
capacity.

## 7. Tearing it down

To delete the VM and start over (e.g. to rebuild step 4 from scratch):

```bash
sudo virsh destroy troshka-libvirt-host    # force off; no-op if already stopped
sudo virsh undefine troshka-libvirt-host --nvram

sudo rm -f /var/lib/libvirt/images/troshka-host.qcow2 \
  /var/lib/libvirt/images/troshka-data.qcow2
```

`virsh destroy` only powers it off — `virsh undefine` is the actual
delete-from-libvirt step. `--import` doesn't hand the disks' lifecycle over
to libvirt either, so `undefine` alone won't remove the qcow2 files; the
`rm` above is still required. `--nvram` is harmless to pass even without
UEFI.

Don't forget to also remove the host from Troshka (`DELETE
/api/v1/hosts/<id>`) so it stops being considered for placement:

```bash
curl -X DELETE http://localhost:8200/api/v1/hosts/<id> \
  -H "Authorization: Bearer $TROSHKA_TOKEN"
```
