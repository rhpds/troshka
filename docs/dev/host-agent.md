# Host Agent (troshkad) Reference

> Extracted from the top-level `CLAUDE.md` to keep it lean. Read this file when working on the topics below.

### Troshkad (Host Agent Daemon)
- Single-file Python daemon at `src/troshkad/troshkad.py` — stdlib only, no pip
- Backend client: `src/backend/app/services/troshkad_client.py` — urllib3 connection pooling with cert fingerprint pinning
- HTTPS on port 31337, mTLS + bearer token auth (two-layer authentication)
- All host operations go through troshkad — SSH only for initial install
- **mTLS**: Global CA (`agent_ca_cert` in `system_config` table) signs a backend client cert. Troshkad requires client certs signed by this CA — unauthenticated connections (scanners, probes) are rejected at TLS handshake before any HTTP processing. CA + client cert generated on first backend startup via `agent_ca_service.py`. CA cert deployed to hosts during agent install at `/opt/troshka/tls/ca.crt`, referenced by `client_ca` in `troshkad.conf`. Backward-compatible: hosts without the CA cert file run without mTLS (token-only auth). Requires **Install Agent** (not Update Agent) to enable on a host.
- **Rate limiting**: Per-IP auto-ban — 10 auth failures in 60s → 5-min ban, 3 temp bans in 1 hour → permaban (until process restart). Banned IPs rejected in `verify_request()` before spawning handler threads. TLS handshake timeout (10s) in `get_request()` prevents stuck handshakes from blocking the accept loop. Backend has matching middleware (`core/rate_limit.py`).
- **NFS resilience**: NFS mounts use `soft,timeo=50,retrans=3` (fail with EIO instead of D-state). Watchdog probes NFS mount health with 5s timeout thread, auto-recovers via lazy unmount + remount after 60s stale. `/health` reports `nfs_stale` status. `_get_capacity` and `_get_partitions` skip NFS paths when stale.
- **troshka-vncd**: separate daemon (`src/troshka-vncd/troshka-vncd.py`) for VNC console relay — port 443, `websockets` library, systemd-managed
- vncd updates pushed via `/admin/update-vncd` endpoint on troshkad, also handled by `update-agent.sh`
- **Qemu hook** (`/etc/libvirt/hooks/qemu`): lives ONLY in agent install script (`agent_deployer.py`), must NOT call `virsh` (deadlocks virtqemud), parses XML from stdin
- **Python string escaping**: backslashes in install script heredocs must be doubled (`\\(`, `\\1`, `\\K`)
- **Shared ISOs**: hard-linked into VM dirs (not symlinked) — prevents qemu permission denied and survives `virsh undefine --remove-all-storage`
- **File ownership**: chown to `qemu:qemu` after creating disks, seeds, and hard links
- **Download locking**: `fcntl.flock()` prevents concurrent downloads of same file
- **Wipe preserves cache**: never deletes `/var/lib/troshka/images/` or `/var/lib/troshka/cache/`
- **Job cancellation**: `DELETE /jobs/{job_id}` sets `_cancelled` flag and kills active subprocess; handlers check `_cancelled` between steps
- **Version**: `VERSION = "dev"` in source, stamped with SHA-256 content hash at push time
- **NIC models**: `virtio`, `e1000`, `e1000e`, `igb` (Intel 82576 SR-IOV emulation), `rtl8139` — set via `model` field in topology NIC data and template YAML
- **powerOnAtDeploy**: per-VM flag in topology — when `false`, VM is defined but not started during deploy (used for blank target VMs like SNOs that boot via BMC/ACM later)
- Agent install restarts `virtqemud` so hook changes take effect
- **Clock offset**: `--clock offset=variable,adjustment=N` added to virt-install when `clock_offset` is in params — sets guest clock to target datetime at the hypervisor level
- **Gateway chronyd**: per-project chronyd runs in the gateway namespace via `ip netns exec` (same pattern as dnsmasq) — config at `/var/lib/troshka/chrony/{pid}.conf`, pidfile at `/run/troshka-chronyd-{pid}.pid`. Killed during `/networks/full-teardown`. Non-fatal if chrony isn't installed on host.
- **`/vms/set-clock` endpoint**: updates `<clock>` element in libvirt XML via `virsh dumpxml` → parse → `virsh define`, then pushes time to running VMs via `virsh domtime` (guest agent) with `virsh qemu-agent-command` fallback
- **Update drain fix**: `_SKIP_DRAIN` set (module-level) lists commands that don't cancel drain or block updates — includes `vm/ssh-exec` and `containers/states` to prevent health poller traffic from cancelling agent updates indefinitely
### Host Operations
- Disk paths: `/var/lib/troshka/vms/{project_id}/{vm_id[:8]}-{disk_id[:8]}.{format}`
- Image cache: `/var/lib/troshka/images/{item_id}.{format}`
- Pattern cache: `/var/lib/troshka/local/cache/patterns/{pattern_id}/` (always local NVMe, never shared NFS — each host downloads from S3)
- Snapshot cache: `/var/lib/troshka/cache/snapshots/{item_id}/`
- PXE boot files: `/var/lib/troshka/pxe/{vni}/tftpboot/` and `/var/lib/troshka/pxe/{vni}/mnt/`
- Domain names: `troshka-{project_id[:8]}-{vm_id[:8]}`
- BMC config: `/var/lib/troshka/bmc/{project_id}/` (sushy configs, vbmcd PID, htpasswd)
- Flatten qcow2 before S3 upload (merge backing chain for standalone images)
### Virtual BMC (IPMI & Redfish)
- Per-VM BMC endpoints: one sushy-emulator + one vbmc per BMC-enabled VM
- BMC tools live in `/opt/troshka/venv/` (sushy-tools, virtualbmc, libvirt-python)
- BMC bridge: `br-bmc-{project_id[:8]}` inside project namespace
- BMC config: `/var/lib/troshka/bmc/{project_id}/` (sushy configs, vbmcd config, htpasswd)
- BMC network node: `networkType: "bmc"` on a networkNode, auto-created when first VM enables BMC
- Credentials stored in topology JSONB (preserved in patterns for lab instruction stability)
- Troshkad endpoints: `/bmc/setup`, `/bmc/teardown`, `/bmc/status`
- Deploy order: BMC setup runs after VM definition but before VM startup
- **SSL**: HTTPS on port 8443 alongside HTTP on port 8000. Self-signed EC P-256 certs generated per VM at setup time. Troshkad runs a second `sushy-emulator` process with SSL config (`sushy-{vm}-ssl.conf`). KubeVirt entrypoint.py runs dual HTTPServer threads. Restore/teardown/status handlers auto-detect SSL files via `sushy-*-ssl.*` glob patterns.
- **BMC data in deployed_topology**: `deployed_topology.bmc` stores per-VM `redfish_url`, `redfish_url_ssl`, `ipmi_address`, username, password. Populated at deploy completion for both troshkad and KubeVirt paths.
### Health Poller & Storage Monitoring
- `health_poller.py` runs periodic checks on all connected hosts
- Reports all mounted partitions via troshkad `/health` endpoint (not just root)
- Evaluates partition thresholds, stores `storage_warnings` JSONB on Host model
- Frontend shows warning badges on hosts admin page when partitions exceed thresholds
- Re-signs host TLS certs hourly, checks CA expiry (renews at 90 days)
- Auto-recovery: when a host reconnects (disconnected → connected), `recover_host_services()` in `gc_service.py` restores networking (namespaces, VXLAN, bridges, dnsmasq, nftables) and BMC (sushy, vbmc) for all active projects via background thread. Deduplicates by host ID.
### Libvirt Events (troshkad)
- Lifecycle events: `VIR_DOMAIN_EVENT_ID_LIFECYCLE` callback for start/stop/crash/reboot detection
- Block threshold events: `VIR_DOMAIN_EVENT_ID_BLOCK_THRESHOLD` for disk usage alerts, auto-re-arms after trigger
- Batch VM state polling: `POST /vms/states` returns all domain states in one call (replaces per-VM polling)
