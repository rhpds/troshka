# Storage, Migration & Garbage Collection Reference

> Extracted from the top-level `CLAUDE.md` to keep it lean. Read this file when working on the topics below.

### Garbage Collector
- Runs on host agent connect, admin Clean button, or future cron
- Steps: capacity sync → orphan cleanup → network repair → cache eviction → S3 cleanup → SharedCacheEntry cleanup
- Cache eviction: cross-references host cache dirs against DB records (patterns + library items), deletes orphaned entries immediately
- Temp dir cleanup: cross-references against running jobs' `_tmpdirs` — anything not owned by a running job is deleted immediately (no age threshold)
- S3 orphan cleanup: `clean_s3_orphans()` scans `patterns/`, `snapshots/`, `library/` prefixes, deletes objects with no matching DB record, aborts stale multipart uploads
- SharedCacheEntry cleanup: deletes DB records pointing to deleted patterns/library items
- Capacity re-sync: re-runs after cache cleanup so counters reflect freed disk space
- Dry-run mode: `reconcile_host(host_id, dry_run=True)` reports what would be cleaned without deleting
### Storage Auto-Extend
- Auto-extend for EBS volumes and FSx file systems when usage exceeds threshold
- Config columns on `storage_pools` and `hosts`: `auto_extend_enabled`, `auto_extend_threshold_pct`, `auto_extend_increment_gb`, `auto_extend_max_gb`
- Manual extend via admin UI (pool page "Extend Now" button) with real-time capacity polling
- FSx has a 6-hour cooldown between extends — backend catches this error and returns a clear message
- `storage_extend.py` service handles both FSx and EBS extend logic
- EBS: `ModifyVolume` API, requires `describe-volumes-modifications` polling
- FSx: `UpdateFileSystem` API with `StorageCapacityReservationGiB`
### Shared Storage & Live Migration
- **Storage pools** group hosts sharing NFS storage — all hosts in a pool can live-migrate VMs between each other
- Three modes: `shared-fsx` (managed FSx OpenZFS), `shared-byo` (user-provided NFS), `local` (default, no pool needed)
- FSx OpenZFS: Single-AZ, LZ4 compression, `nconnect=16`, `cache=none,io=native` for VM disks
- Per-second billing, no minimum commitment (~$53/month for 128 GB/160 MBps)
- Hosts without a `storage_pool_id` operate in local mode (backward compatible)
- **Download coordination**: `SharedCacheEntry` tracks what's cached on shared storage — one download serves all hosts in the pool
- **Migration**: `virsh migrate --persistent --undefinesource` via troshkad `vm/migrate` endpoint; `--live` added for running VMs, omitted for stopped VMs (cold migration)
- Migration uses **private IP** for intra-VPC traffic (Host.private_ip field)
- Migration orchestration: set up networks/BMC on target → migrate VMs in start order → tear down source
- **Host evacuation**: moves all projects off a host to other hosts in the same pool
- **Path resolution**: troshkad `_storage_path()` routes to `/var/lib/troshka/shared/` or `/var/lib/troshka/local/` based on `storage_mode` config
- **Pool-level GC**: cache eviction uses `SharedCacheEntry` table, checks all projects in pool before evicting
- BYO NFS pools don't require an AZ or provider — user manages their own NFS infrastructure
- Security group rules: NFS (TCP 2049) for FSx, libvirt TLS (TCP 16514) + migration data (TCP 49152-49215) for all shared pools
- **PKI**: pool-level CA (10-year, stored on StoragePool.ca_cert/ca_key), host certs signed with both public+private IPs as SANs (1-year, re-signed hourly by health poller)
- Libvirt TLS: mutual TLS with pool CA verification, no `tls_no_verify_certificate`
- Auto-renewal: health poller checks CA expiry (renews at 90 days), re-signs and pushes host certs hourly via troshkad `tls/update-certs` endpoint
- Provider credentials mapping: use `_boto_client()` helper — `get_credentials()` returns `access_key_id` which must be mapped to `aws_access_key_id` for boto3
- **Placement**: auto-selects pool with most free RAM, syncs capacity before placing, sorts by least-loaded host. Admins can override pool at deploy time.
