# Networking & Console Reference

> Extracted from the top-level `CLAUDE.md` to keep it lean. Read this file when working on the topics below.

### Console (Direct Proxy)
- VNC console at `/console?vm=&project=&name=` — bare layout (no app header)
- **Direct proxy**: Browser → `wss://{instance_id}.{base_domain}/ws/{jwt}` → troshka-vncd → localhost VNC (2 hops, no SSH tunnel)
- Backend issues a short-lived JWT (5 min, single-use) signed with the host's agent token
- `troshka-vncd` daemon on each host validates JWT, resolves VNC port via `virsh dumpxml`, proxies binary frames
- TLS via Let's Encrypt (certbot DNS-01 challenge with Route53 instance profile)
- noVNC (`@novnc/novnc`), `focusOnClick=true`
- Virtual keyboard at `/console/keyboard?name=` — popup via `window.open()`
- Keyboard communicates via `postMessage` with same-origin restriction (never `"*"`)
- Key macros: Linux/Windows dropdowns send X11 keysyms via `sendCombo()`
- `sendCombo()`: press all keys down in order, release in reverse — standard VNC key combo pattern
### Console Route53 Setup
- Fully automated from admin UI: Providers page → "Setup Console" → enter domain → done
- Console config stored on Provider model (`console_zone_id`, `console_base_domain`, `console_nameservers`)
- `POST /providers/{id}/setup-console` creates Route53 hosted zone + IAM role/instance profile
- `DELETE /providers/{id}/console` removes hosted zone, clears DNS records and host `console_domain` fields
- Each host gets an A record: `{instance_id}.{base_domain}` → public IP (created during provisioning, deleted on removal)
- NS delegation: UI shows nameservers in a collapsible section — admin adds NS records in parent zone
- IAM: `troshka-certbot-role` + `troshka-certbot-profile` created by setup-console endpoint (idempotent)
- Instance profile attached to EC2 instances — allows certbot DNS-01 without storing AWS creds on hosts
- certbot installed in `/opt/troshka/venv/`, certs at `/etc/letsencrypt/live/{fqdn}/`
- Auto-renewal via cron: `certbot renew --quiet`
- `console_domain` stored on Host model, set during provisioning
- **No config.yaml** — console config lives on the Provider, not in config files
- **IAM policy note**: `route53:GetChange` requires `Resource: "*"` (not scoped to hosted zone)
### OCP Route External Access (OCP Virt)
- OCP Virt hosts use OCP Routes instead of EIPs for external access to VMs
- Deploy creates edge-terminated Routes for port 443/80 forwards: `{vm_name}-{port}.apps.{cluster_domain}`
- Route annotation: `haproxy.router.openshift.io/timeout: 3600s` (required for WebSocket consoles)
- Routes are cleaned up during project destroy
- EIP allocation is skipped when all port forwards are routable via Routes
### Multi-Host Network Mesh
- Projects that exceed a single host's capacity (or use anti-affinity) are deployed across multiple hosts
- **WireGuard mesh**: per-project encrypted tunnels between hosts, VXLAN runs inside for L2 adjacency
- **Model**: `ProjectMeshPeer` stores WireGuard keys/endpoints/addresses per host per project
- **Placement**: `find_multihost_placement()` bin-packs VMs with affinity group support; `select_network_host()` picks the host running dnsmasq/nftables
- **Affinity**: `affinityGroup` on VM node data — VMs with the same group land on the same host
- **Anti-affinity**: `separateHost` (group name) on VM node data — VMs with the same group land on different hosts. Forces multi-host even when one host has capacity. Auto-finds a pool with 2+ hosts if no provider set.
- **Network host**: one host runs dnsmasq/chronyd/nftables (full namespace); remote hosts have VXLAN+bridge only via `/mesh/join-network`
- **Private IPs**: same-pool hosts use `private_ip` for WireGuard endpoints (pod network), not public IPs
- **Data format**: `project.host_assignments` is `{vm_node_id: host_id}` (flattened). Deploy service unflattens to `{host_id: [vm_ids]}` for orchestration.
- **Deploy flow**: placement → mesh setup → network host setup → remote VXLAN → per-host image cache → seeds → disks → VM define → VM start
- **Teardown**: stop VMs all hosts → teardown remote VXLAN → teardown network host → teardown WireGuard → delete mesh peers
- **FDB entries**: must be added AFTER `ip link set vxlan netns` — moving interface to namespace clears host-namespace FDB
- **Stale domains**: multi-host deploy cleans up stale domains before VM creation (same as single-host)
- Troshkad endpoints: `POST /commands/mesh/setup`, `POST /commands/mesh/join-network`, `DELETE /mesh/teardown`, `GET /mesh/status`
- Security group rules: WireGuard UDP 51820-51850 added to AWS SG, GCP firewall, Azure NSG
- `wireguard-tools` package in agent install (dnf install is always run, no conditional)
### PXE Network Boot
- Firmware (BIOS/UEFI) and Secure Boot are per-VM settings, not per-network
- Two modes: **Troshka managed** (auto-extracts kernel/initrd from library ISO) and **BYO** (user provides boot server)
- Managed mode: VM selects an install ISO via `pxeBootIsoId` on the VM node data
- Deploy flow: cache ISO → extract kernel/initrd with `isoinfo` → enable dnsmasq TFTP → start HTTP server for install source
- PXE boot files: `/var/lib/troshka/pxe/{vni}/tftpboot/` (kernel, initrd, pxelinux.0, pxelinux.cfg/default)
- ISO mount: `/var/lib/troshka/pxe/{vni}/mnt/` (loop-mounted read-only, served via HTTP)
- HTTP install source port: `8080 + (vni % 1000)`, deterministic per network
- Troshkad handler: `/pxe/setup` (extract + mount + serve), cleaned up by `/networks/full-teardown`
- Auto-detects kernel/initrd paths for RHEL, Ubuntu, Debian, SLES ISOs
- The deploy path reads PXE config from topology JSONB, not from Network model/schemas
- `virt-install --boot uefi` for UEFI VMs; `firmware.feature0` flags for Secure Boot
### OC-Exec DNS Resolution
- **Problem**: `ip netns exec` runs `oc` inside the project namespace, but the namespace inherits the host's `/etc/resolv.conf` — can't resolve project-internal domains like `api.ocp.ocp.local`
- **Troshkad fix**: `unshare --mount` creates private mount namespace, bind-mounts custom resolv.conf pointing at project dnsmasq gateway IP. Gateway IP auto-detected from namespace bridge IPs.
- **KubeVirt fix**: exec pod already has correct DNS via `dnsConfig` in pod spec
- **Shell pipelines** (`|`, `&&`, `;`) skip `_exec_oc` and fall through to bastion SSH
- **Bastionless**: simple `oc` commands work without a bastion on all providers
