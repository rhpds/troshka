# IBI Standalone Phase 1 — Get It Working

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Get standalone IBI (Image-Based Installation) deploying an SNO cluster end-to-end on Troshka — seed build through cluster-ready — using Redfish throughout.

**Architecture:** A bastion VM generates two ISOs (installation + config) using `openshift-install`. The bastion boots a blank target VM from the installation ISO via Redfish BMC, waits for seed restore, then swaps in the config ISO for reconfiguration. All BMC operations use sushy-emulator's Redfish API. Pull-through registry avoids quay.io throttling.

**Tech Stack:** Ansible (agnosticd-v2 role), Jinja2 templates, openshift-install CLI, Redfish API (sushy-emulator), lca-cli (Lifecycle Agent), RHCOS

## Global Constraints

- All BMC operations via Redfish — no troshkad workarounds, no `virsh` calls from ansible
- Pull-through registry required: `registry-quay-quay-enterprise.apps.ocpv-infra01.dal12.infra.demo.redhat.com`
- Seed image: `quay.io/redhat-gpte/sno-seed:4.22` (OCP 4.22.0)
- Target VM layout: vda=200GB boot, vdb=250GB containers, UEFI, BMC enabled
- Config ISO volume label must be `cluster-config` (hardcoded in openshift-install)
- No Troshka backend/Python changes — all work in ansible role, templates, and test scripts
- Three repos: troshka (`/Users/prutledg/troshka`), agnosticd-v2 (`~/agnosticd-v2`), agnosticv (`~/agnosticv`)
- Common password: `{{ (guid[:5] | hash('md5') | int(base=16) | b64encode)[:8] }}`
- Test with: `./scripts/test-ibi-deploy.sh`

---

### Task 1: Add debugging access via ignition override

The `image-based-installation-config.yaml` supports `ignitionConfigOverride` — a JSON string merged into the live ISO ignition. This sets a root password on the seed-restored RHCOS system so we can log in via VNC console to debug post-restore issues.

**Files:**
- Modify: `~/agnosticd-v2/ansible/roles/host_ocp4_ibi_installer/defaults/main.yml`
- Modify: `~/agnosticd-v2/ansible/roles/host_ocp4_ibi_installer/templates/image-based-installation-config.yaml.j2`

**Produces:**
- `host_ocp4_ibi_debug_password` variable (default: `""`, set to a password string to enable)
- Installation ISO ignition includes `passwd` section with hashed root password when debug password is set

- [ ] **Step 1: Add debug password default to role defaults**

In `~/agnosticd-v2/ansible/roles/host_ocp4_ibi_installer/defaults/main.yml`, add at the end:

```yaml
# Debug root password — set to a plaintext password to enable VNC console login
# on the seed-restored RHCOS system. Leave empty for production.
host_ocp4_ibi_debug_password: ""
```

- [ ] **Step 2: Add ignition override generation task to main.yml**

In `~/agnosticd-v2/ansible/roles/host_ocp4_ibi_installer/tasks/main.yml`, add this task block **before** the "Generate image-based-installation-config.yaml" task (currently at line 115):

```yaml
    - name: Generate ignition config override for debugging
      when: host_ocp4_ibi_debug_password | default('') | length > 0
      block:
        - name: Hash debug password
          ansible.builtin.shell:
            cmd: >-
              python3 -c "import crypt; print(crypt.crypt('{{ host_ocp4_ibi_debug_password }}', crypt.mksalt(crypt.METHOD_SHA512)))"
          register: _ibi_password_hash
          changed_when: false
          no_log: true

        - name: Build ignition override JSON
          ansible.builtin.set_fact:
            _ibi_ignition_override: >-
              {"ignition":{"version":"3.2.0"},"passwd":{"users":[{"name":"root","passwordHash":"{{ _ibi_password_hash.stdout }}"}]}}
          no_log: true
```

- [ ] **Step 3: Update installation config template to include ignition override**

Replace the contents of `~/agnosticd-v2/ansible/roles/host_ocp4_ibi_installer/templates/image-based-installation-config.yaml.j2` with:

```yaml
apiVersion: v1beta1
kind: ImageBasedInstallationConfig
metadata:
  name: ibi-config
seedImage: {{ host_ocp4_ibi_seed_image }}
seedVersion: "{{ host_ocp4_ibi_seed_version }}"
installationDisk: {{ host_ocp4_ibi_installation_disk }}
pullSecret: '{{ host_ocp4_pull_secret }}'
{% if host_ocp4_ibi_extra_partition_start | default('') | length > 0 %}
extraPartitionStart: "{{ host_ocp4_ibi_extra_partition_start }}"
{% endif %}
{% if host_ocp4_ssh_key | length > 0 %}
sshKey: '{{ host_ocp4_ssh_key }}'
{% endif %}
{% if _ibi_network_config is defined %}
networkConfig:
{{ _ibi_network_config | indent(2, first=true) }}
{% endif %}
{% if _ibi_ignition_override is defined %}
ignitionConfigOverride: '{{ _ibi_ignition_override }}'
{% endif %}
{% if host_ocp4_ibi_image_digest_sources | default([]) | length > 0 %}
imageDigestSources:
{% for source in host_ocp4_ibi_image_digest_sources %}
  - source: "{{ source.source }}"
    mirrors:
{% for mirror in source.mirrors %}
      - "{{ mirror }}"
{% endfor %}
{% endfor %}
{% endif %}
```

- [ ] **Step 4: Set debug password in agnosticv common.yaml for testing**

In `~/agnosticv/troshka/OCP4-SNO-IBI/common.yaml`, add:

```yaml
host_ocp4_ibi_debug_password: "{{ common_password }}"
```

This uses the per-GUID generated password so it's unique per deploy but predictable for the operator.

- [ ] **Step 5: Commit**

```bash
cd ~/agnosticd-v2 && git add ansible/roles/host_ocp4_ibi_installer/ && git commit -m "feat(ibi): add debug root password via ignition override"
cd ~/agnosticv && git add troshka/OCP4-SNO-IBI/common.yaml && git commit -m "feat(ibi): enable debug password for IBI testing"
```

---

### Task 2: Add pull-through registry support to installation ISO

The installation ISO's `image-based-installation-config.yaml` supports `imageDigestSources` for registry mirroring. This tells `lca-cli` to use the pull-through registry when pulling the seed image and pre-caching container images during seed restore.

**Files:**
- Modify: `~/agnosticd-v2/ansible/roles/host_ocp4_ibi_installer/defaults/main.yml`
- Modify: `~/agnosticd-v2/ansible/roles/host_ocp4_ibi_installer/tasks/main.yml`

**Consumes:**
- `pull_through_registry` dict from agnosticv common.yaml (has `.enabled`, `.url`, `.orgs` mapping)
- `host_ocp4_ibi_image_digest_sources` list (produced by new task)

**Produces:**
- `host_ocp4_ibi_image_digest_sources` list for `imageDigestSources` in installation ISO template (already wired in Task 1)

- [ ] **Step 1: Add imageDigestSources default**

In `~/agnosticd-v2/ansible/roles/host_ocp4_ibi_installer/defaults/main.yml`, add:

```yaml
# Image digest sources for pull-through registry mirroring
# Format: [{source: "registry.redhat.io", mirrors: ["mirror.example.com/registry_redhat_io"]}]
host_ocp4_ibi_image_digest_sources: []
```

- [ ] **Step 2: Add task to build imageDigestSources from pull_through_registry**

In `~/agnosticd-v2/ansible/roles/host_ocp4_ibi_installer/tasks/main.yml`, add this block **before** the ignition override block from Task 1 (so it's available when building the override):

```yaml
    - name: Build image digest sources from pull-through registry config
      when: pull_through_registry is defined and pull_through_registry.enabled | default(false)
      block:
        - name: Set image digest sources for pull-through registry
          ansible.builtin.set_fact:
            host_ocp4_ibi_image_digest_sources: >-
              {{ host_ocp4_ibi_image_digest_sources | default([]) +
                 [{'source': item.key, 'mirrors': [pull_through_registry.url ~ '/' ~ item.value]}] }}
          loop: "{{ pull_through_registry.orgs | dict2items }}"
```

- [ ] **Step 3: Commit**

```bash
cd ~/agnosticd-v2 && git add ansible/roles/host_ocp4_ibi_installer/ && git commit -m "feat(ibi): add pull-through registry support via imageDigestSources"
```

---

### Task 3: Fix the deploy flow sequencing

The current ansible role has the right steps but the ForceOff → EjectMedia → InsertMedia → ForceOn sequence needs to be airtight. Review and fix the Phase 3 sequencing so the config ISO is definitely attached and the VM boots from disk with the config ISO visible as `/dev/sr0`.

**Files:**
- Modify: `~/agnosticd-v2/ansible/roles/host_ocp4_ibi_installer/tasks/main.yml`
- Modify: `~/agnosticd-v2/ansible/roles/host_ocp4_ibi_installer/tasks/boot_via_bmc.yml`

**Key fix:** The current flow does ForceOff → EjectMedia → InsertMedia+ForceOn (boot_via_bmc). But `boot_via_bmc.yml` does InsertMedia then immediately Reset. For the config ISO phase, we need InsertMedia **without** Reset, then a separate ForceOn — because `boot_via_bmc` uses ForceRestart for running VMs, but the VM is already off after ForceOff.

Actually, looking at `boot_via_bmc.yml` more carefully, it checks `PowerState` and uses ForceOn if the VM is Off. So the current flow should work: ForceOff → wait → EjectMedia → call boot_via_bmc (which inserts config ISO + ForceOn since VM is Off). The issue is that the **wait after ForceOff may not be long enough** (currently 10s pause) — and sushy may not report PowerState=Off immediately.

- [ ] **Step 1: Improve ForceOff wait with polling**

Replace the fixed 10-second pause after ForceOff with a Redfish power state poll in `~/agnosticd-v2/ansible/roles/host_ocp4_ibi_installer/tasks/main.yml`. Replace:

```yaml
    - name: Wait for VM to power off
      ansible.builtin.pause:
        seconds: 10
```

with:

```yaml
    - name: Wait for VM to power off
      ansible.builtin.uri:
        url: "http://{{ _bmc_ip_list[0] }}:{{ _ibi_bmc_port }}/redfish/v1/Systems/{{ _sys_for_off.json.Members[0]['@odata.id'].split('/')[-1] }}"
        user: "{{ host_ocp4_bmc_user }}"
        password: "{{ host_ocp4_bmc_password }}"
        force_basic_auth: true
        return_content: true
      register: _sys_power_check
      until: _sys_power_check.json.PowerState | default('On') == 'Off'
      retries: 30
      delay: 5
      changed_when: false
```

- [ ] **Step 2: Add progress logging between phases**

Add debug tasks between major phases in `main.yml` for visibility:

```yaml
    - name: "IBI Phase 3: Swapping ISOs — eject install, attach config"
      ansible.builtin.debug:
        msg: "Ejecting installation ISO, attaching config ISO ({{ host_ocp4_ibi_config_dir }}/imagebasedconfig.iso)"
```

Add before the "Eject any existing virtual media before config ISO" task.

```yaml
    - name: "IBI Phase 4: Waiting for cluster"
      ansible.builtin.debug:
        msg: "VM booted from disk with config ISO attached. Waiting for lca-cli reconfiguration + cluster ready..."
```

Add before the "Wait for OCP cluster to come up" task.

- [ ] **Step 3: Commit**

```bash
cd ~/agnosticd-v2 && git add ansible/roles/host_ocp4_ibi_installer/ && git commit -m "fix(ibi): improve ForceOff wait with power state polling"
```

---

### Task 4: Run clean end-to-end deploy and diagnose

This is a manual execution + diagnosis task. Run the test script and observe what happens. No code to write — just running the deploy and capturing diagnostic output.

**Files:**
- None modified (diagnostic only)

**Prerequisites:**
- Tasks 1-3 committed
- Troshka backend running at localhost:8200
- Seed image available at `quay.io/redhat-gpte/sno-seed:4.22`
- All secrets in place (`~/secrets/troshka-api-key.txt`, `~/secrets/ocp4-pull-secret.json`, `~/secrets/troshka-pull-through-registry.yaml`)

- [ ] **Step 1: Run the test deploy**

```bash
cd /Users/prutledg/troshka
./scripts/test-ibi-deploy.sh
```

Monitor the output. Expected phases:
1. Infra deploy (~5 min) — bastion + cp-0 VMs created
2. Installation ISO generation (~1 min)
3. Boot from installation ISO via Redfish
4. Seed restore (~10-15 min) — watch for "IBI preparation process finished"
5. ISO swap (eject install ISO, attach config ISO, ForceOff/ForceOn)
6. Wait for cluster (~5-15 min)

- [ ] **Step 2: If cluster doesn't come up — diagnose from VNC console**

Open the VNC console for cp-0 from the Troshka UI. Log in as `root` with the debug password (same as `common_password` for this GUID).

Run these diagnostic commands:

```bash
# Check vdb state
lsblk
mount | grep containers
systemctl status var-lib-containers.mount
systemctl status systemd-mkfs@dev-vdb.service
journalctl -u var-lib-containers.mount --no-pager

# Check lca-cli post-pivot state
systemctl list-units | grep lca
journalctl -b 0 | grep -i "lca-cli\|cluster-config\|recert\|post-pivot" | tail -50

# Check if config ISO is visible
blkid | grep cluster-config
ls /dev/sr0

# Check crio/kubelet
systemctl status crio
systemctl status kubelet
```

- [ ] **Step 3: Record findings**

Based on the diagnostic output, determine:
1. Is vdb mounted at `/var/lib/containers`? If not, what failed?
2. Is the config ISO visible at `/dev/sr0` with label `cluster-config`?
3. Is the lca-cli post-pivot service running? What does it report?
4. Are crio and kubelet running?

Document findings for Task 5.

---

### Task 5: Fix vdb mount (based on diagnosis)

This task has two branches depending on what Task 4 reveals. Implement whichever applies.

**Files:**
- Modify: `~/agnosticd-v2/ansible/roles/host_ocp4_ibi_installer/tasks/main.yml` (Branch A only)
- Modify: `~/agnosticd-v2/ansible/roles/host_ocp4_ibi_installer/templates/image-based-installation-config.yaml.j2` (Branch B only)
- Modify: `~/agnosticd-v2/ansible/roles/host_ocp4_ibi_installer/defaults/main.yml` (Branch B only)

#### Branch A: MachineConfig units exist but fail

If `systemctl status var-lib-containers.mount` shows the unit exists but failed, the fix is likely a systemd ordering issue. Common causes:
- `BindsTo=dev-vdb.device` fires before udev settles the device
- The mkfs service runs but the disk path is wrong

Fix: Add a task to pre-format vdb from the bastion via SSH **during the live ISO phase** (before the VM reboots). After the "Wait for IBI seed restore to complete" task, add:

```yaml
    - name: Pre-format vdb on target during live ISO phase
      ansible.builtin.shell:
        cmd: >-
          ssh -i /home/{{ ansible_user }}/.ssh/ibi_key
          -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5
          core@{{ _ibi_target_ip }}
          "sudo bash -c 'if ! blkid /dev/vdb | grep -q xfs; then mkfs.xfs -f /dev/vdb; echo FORMATTED; else echo ALREADY_XFS; fi'"
      register: _ibi_vdb_format
      changed_when: "'FORMATTED' in _ibi_vdb_format.stdout"
      failed_when: false
```

#### Branch B: MachineConfig units don't exist in restored ostree

If the systemd units for vdb don't exist at all, the seed capture stripped them. Fix: use the `ignitionConfigOverride` on the installation ISO to inject the mount units.

Add to `defaults/main.yml`:

```yaml
# Container storage device — must match seed metadata container_storage_mountpoint_target
host_ocp4_ibi_container_storage_device: /dev/vdb
```

Extend the ignition override JSON construction in `main.yml` (the block from Task 1) to include systemd units for vdb:

```yaml
        - name: Build ignition override JSON
          ansible.builtin.set_fact:
            _ibi_ignition_override: >-
              {"ignition":{"version":"3.2.0"},
              "passwd":{"users":[{"name":"root","passwordHash":"{{ _ibi_password_hash.stdout | default('*') }}"}]},
              "systemd":{"units":[
              {"name":"format-vdb.service","enabled":true,"contents":"[Unit]\nDescription=Format {{ host_ocp4_ibi_container_storage_device }} for containers\nBefore=var-lib-containers.mount\nConditionFirstBoot=yes\n\n[Service]\nType=oneshot\nExecStart=/usr/sbin/mkfs.xfs -f {{ host_ocp4_ibi_container_storage_device }}\n\n[Install]\nWantedBy=multi-user.target"},
              {"name":"var-lib-containers.mount","enabled":true,"contents":"[Unit]\nDescription=Mount {{ host_ocp4_ibi_container_storage_device }} for containers\nBefore=local-fs.target\nAfter=format-vdb.service\n\n[Mount]\nWhat={{ host_ocp4_ibi_container_storage_device }}\nWhere=/var/lib/containers\nType=xfs\nOptions=defaults,prjquota\n\n[Install]\nWantedBy=local-fs.target"}
              ]}}
          no_log: true
```

**Note:** Only implement the branch that matches the diagnosis. If Branch B is needed, the ignition override from Task 1 should be merged with the vdb units (they go in the same JSON object).

- [ ] **Step 1: Implement the appropriate fix based on Task 4 findings**

Apply Branch A or Branch B as described above.

- [ ] **Step 2: Re-run the test deploy**

Destroy the previous IBI project first:

```bash
cd /Users/prutledg/troshka
./scripts/test-agnosticd-flow.sh --destroy --guid <GUID_FROM_TASK_4>
```

Then re-run:

```bash
./scripts/test-ibi-deploy.sh
```

- [ ] **Step 3: Verify vdb is mounted**

From VNC console (or SSH if the cluster comes up):

```bash
mount | grep containers
df -h /var/lib/containers
```

Expected: `/dev/vdb` mounted at `/var/lib/containers` as XFS.

- [ ] **Step 4: Commit the fix**

```bash
cd ~/agnosticd-v2 && git add ansible/roles/host_ocp4_ibi_installer/ && git commit -m "fix(ibi): resolve vdb /var/lib/containers mount on target VM"
```

---

### Task 6: Verify full flow and update documentation

After Tasks 4-5 resolve the blockers, verify the complete flow works end-to-end and update the status doc.

**Files:**
- Modify: `/Users/prutledg/troshka/docs/ibi-status.md`

- [ ] **Step 1: Verify cluster is available**

From bastion (SSH via `vm-ssh.sh` or `vm-exec.sh`):

```bash
oc get clusterversion
oc get nodes
oc get co
```

All cluster operators should be Available=True, not Degraded.

- [ ] **Step 2: Verify console access**

Check the OCP web console is reachable:

```bash
curl -k https://console-openshift-console.apps.sno.sno.local
```

(This goes through the bastion's DNS resolution.)

- [ ] **Step 3: Update ibi-status.md**

Replace the contents of `/Users/prutledg/troshka/docs/ibi-status.md` with the current state — what works, what was fixed, the verified flow, and any remaining issues. Include:

- The sushy-emulator CDROM persistence finding (it works, no patching needed)
- The boot order interaction (safe for blank VMs)
- The vdb fix (whichever branch was applied)
- The debugging access method (root password via ignition override)
- The pull-through registry integration
- The complete working flow with timings

- [ ] **Step 4: Commit**

```bash
cd /Users/prutledg/troshka && git add docs/ibi-status.md && git commit -m "docs: update IBI status with working flow and investigation findings"
```
