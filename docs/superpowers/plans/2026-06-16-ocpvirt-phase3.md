# OCP Virt Phase 3 — EIPs, Console, Polish

**Status:** Ready for next session
**Prereq:** Phase 2 complete — host provisioning, agent install, deploy all working on ocpvdev01

---

## What works now
- Provider abstraction (EC2 + OCP Virt drivers)
- Host provisioning via KubeVirt VMs with MetalLB external IPs
- SSH agent install on port 22000 with cloud-user
- RHEL 10 DVD ISO for local repos (BaseOS + AppStream)
- Dedicated data disk (/dev/vdb → /var/lib/troshka)
- SELinux virt_image_t context on data disk
- Projects deploy and nested VMs boot
- Host card shows VMs running/defined, Projects running/total
- troshkad connected and healthy

## Remaining work

### 1. EIPs via MetalLB (major feature)
**Goal:** External IP allocation for nested VMs, same UX as EC2 EIPs.

**Approach:** Each EIP = a LoadBalancer Service with a dedicated MetalLB IP. nftables DNAT inside the host VM forwards ports from the masquerade IP to nested VMs (same mechanism as EC2).

**Tasks:**
- Abstract EIP allocation into provider driver (`base.py` interface)
- `EC2Driver`: current `allocate_address`/`associate_address` logic (extract from `eip_service.py`)
- `OCPVirtDriver`: create LoadBalancer Service per EIP, read assigned IP from status
- Update `eip_service.py` to use driver instead of direct boto3
- Update `deploy_service.py` external access flow — gate on provider, use driver for IP allocation
- troshkad nftables rules: already provider-agnostic (they just DNAT from the host's perspective)
- Frontend: re-enable external access toggle for OCP Virt projects
- Test: deploy with external access, verify port forwarding works through MetalLB IP

**Open questions:**
- One LB Service per EIP, or one LB Service with multiple ports per project?
- MetalLB pool size limit (125 IPs) — how many EIPs can we allocate?
- Cleanup: delete LB Services when project is undeployed

### 2. Console via MetalLB (fix current host)
**Goal:** VNC console connects through the MetalLB IP on port 443.

**What's needed:**
- Port 443 already added to LB Service spec and masquerade ports (for new hosts)
- Current host needs reprovision to get port 443 in masquerade mapping
- vncd starts automatically when console_domain is set (fixed in auto-install)
- Test: open console on a VM, verify WebSocket connects through MetalLB IP
- The console URL will be `wss://67.228.103.5/ws/{jwt}` — no DNS, just IP with self-signed cert

### 3. Agent install reliability
**Tasks:**
- Increase `wait_for_ssh` timeout for OCP Virt (cloud-init + package install takes 3-5 min)
- Wait for cloud-init to finish before running deploy_agent (check `cloud-init status --wait`)
- Or: embed more of the install in cloud-init runcmd so SSH deploy has less to do
- Fix: agent deploy should wait for cloud-init package install to complete before running the install script

### 4. Polish / bugs
- EBS-specific code in agent install script (NVMe detection, /dev/sdf) — skip gracefully on OCP Virt
- `used_vcpus` shows 0 even with running VMs — health poller not picking up capacity from troshkad (might need agent update)
- Update agent on OCP Virt host (push new troshkad.py) — update-agent.sh needs SSH port support
- Console Route RBAC — if we keep Routes as an option, need `routes/custom-host` permission
- Provider delete should clean up all k8s resources (VMs, Services, Secrets, PVCs)
- The "compute" label on host summary region cards (separate from host card)

### 5. Storage pool (Ceph-NFS)
- Create shared-ceph-nfs pool from UI
- Test NFS mount inside host VM
- Shared image cache across OCP Virt hosts
- Live migration between OCP Virt hosts (requires libvirt TLS PKI setup)

### 6. Pattern buffer on OCP Virt
- Provision pattern buffer VM (separate from compute hosts)
- NBD capture through MetalLB IP
- S3 upload from pattern buffer

---

## Dev cluster reference
- **Cluster:** ocpvdev01.dal13.infra.demo.redhat.com
- **SA:** troshka (namespace: troshka)
- **Token:** `oc create token troshka -n troshka --duration=8760h`
- **MetalLB pool:** 67.228.103.2-126 (125 IPs, auto-assign)
- **SSH:** `ssh -p 22000 -i /tmp/ocpvirt-key.pem cloud-user@67.228.103.5`
- **ISO:** rhel-10.2-dvd-iso (PVC in troshka namespace)
- **Image:** rhel10-kvm DataSource in openshift-virtualization-os-images
