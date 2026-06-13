"""Host garbage collector — reconcile DB state with host reality."""
import logging
import time

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def sync_host_capacity(db: Session, host) -> dict:
    """Recalculate used_vcpus and used_ram_mb from active projects."""
    from app.models.project import Project

    old = {"used_vcpus": host.used_vcpus, "used_ram_mb": host.used_ram_mb}

    total_vcpus = 0
    total_ram_mb = 0
    for p in db.query(Project).filter(
        Project.host_id == host.id,
        Project.state.in_(["active", "stopped"]),
    ).all():
        topo = p.deployed_topology or p.topology or {}
        for n in topo.get("nodes", []):
            if n.get("type") == "vmNode":
                d = n.get("data", {})
                total_vcpus += d.get("vcpus", 0)
                total_ram_mb += d.get("ram", 0) * 1024

    host.used_vcpus = total_vcpus
    host.used_ram_mb = total_ram_mb
    db.commit()

    new = {"used_vcpus": total_vcpus, "used_ram_mb": total_ram_mb}
    changed = old != new
    if changed:
        log.info("Host %s capacity synced: %s -> %s", host.id[:8], old, new)

    return {"old": old, "new": new, "changed": changed}


def discover_orphans(db: Session, host) -> dict:
    """Discover orphaned resources on host via troshkad."""
    from app.models.project import Project
    from app.services.troshkad_client import start_job, wait_for_job

    if not host.ip_address or host.agent_status != "connected":
        return {"error": "Host not reachable", "orphaned_projects": [], "orphaned_domains": [], "orphaned_bridges": []}

    # Build list of known project IDs and domains for GC
    active_project_ids = []
    known_domains = []
    skip_states = {"deploying", "reconfiguring"}

    for p in db.query(Project).filter(Project.host_id == host.id).all():
        if p.state in skip_states or p.state in ("active", "stopped"):
            active_project_ids.append(p.id)
            # Add domain name patterns for this project
            pid_short = p.id[:8]
            known_domains.append(f"troshka-{pid_short}")

    # Build list of project IDs that should have BMC
    bmc_project_ids = set()
    for p in db.query(Project).filter(Project.host_id == host.id).all():
        if p.state in ("active", "stopped"):
            topo = p.deployed_topology or p.topology or {}
            for node in topo.get("nodes", []):
                if node.get("type") == "networkNode" and node.get("data", {}).get("networkType") == "bmc":
                    bmc_project_ids.add(p.id)
                    break

    # Call troshkad to discover orphans
    job_id = start_job(host, "/gc/discover", {
        "known_project_ids": active_project_ids,
        "known_domains": known_domains,
        "known_bmc_project_ids": list(bmc_project_ids),
    })
    job = wait_for_job(host, job_id, timeout=30)
    if job["status"] == "failed":
        return {"error": job["result"].get("error", "Discovery failed")}

    return job["result"]


def clean_orphans(host, orphans: dict) -> dict:
    """Clean orphaned resources on host via troshkad."""
    from app.services.troshkad_client import start_job, wait_for_job

    if not host.ip_address or host.agent_status != "connected":
        return {"error": "Host not reachable", "cleaned": 0}

    # Convert cache items format if needed
    cache_items = []
    for entry in orphans.get("stale_cache", []):
        if isinstance(entry, dict) and "path" in entry:
            cache_items.append(entry["path"])
    for path in orphans.get("orphaned_cache", []):
        if isinstance(path, str):
            cache_items.append(path)

    job_id = start_job(host, "/gc/clean", {
        "orphan_dirs": list(set(orphans.get("orphan_dirs", []))),
        "orphan_domains": list(set(orphans.get("orphan_domains", []))),
        "orphan_bridges": orphans.get("orphan_bridges", []),
        "orphan_namespaces": orphans.get("orphan_namespaces", []),
        "cache_items": cache_items,
        "orphan_bmc_project_ids": orphans.get("orphaned_bmc_project_ids", []),
    })
    job = wait_for_job(host, job_id, timeout=120)

    cleaned = (len(orphans.get("orphan_dirs", [])) + len(orphans.get("orphan_domains", []))
               + len(orphans.get("orphan_bridges", [])) + len(orphans.get("orphan_namespaces", []))
               + len(orphans.get("orphaned_bmc_project_ids", [])))
    return {
        "success": job["status"] == "completed",
        "cleaned": cleaned,
        "output": "\n".join(job.get("output", [])),
    }


def repair_networks(db: Session, host) -> dict:
    """Ensure VXLAN bridges exist for all active/stopped projects on this host."""
    from app.models.project import Project
    from app.services.deploy_service import _setup_networks_via_troshkad

    if not host.ip_address or host.agent_status != "connected":
        return {"repaired": 0, "error": "Host not reachable"}

    projects = db.query(Project).filter(
        Project.host_id == host.id,
        Project.state.in_(["active", "stopped"]),
    ).all()

    if not projects:
        return {"repaired": 0}

    needed_vnis = set()
    for p in projects:
        for vni in (p.vni_map or {}).values():
            needed_vnis.add(str(vni))

    if not needed_vnis:
        return {"repaired": 0}

    # Repair by re-running full network setup for each project with missing bridges
    repaired = 0
    for p in projects:
        project_vnis = set(str(v) for v in (p.vni_map or {}).values())
        if not project_vnis:
            continue
        topo = p.deployed_topology or p.topology or {}
        result = _setup_networks_via_troshkad(host, topo, p.vni_map or {}, db, p.id)
        if result is True:
            repaired += len(project_vnis)
            log.info("Repaired bridges for project %s", p.id[:8])
        else:
            log.warning("Failed to repair bridges for project %s: %s", p.id[:8], result)

    return {"repaired": repaired}


def clean_s3_orphan_patterns(db: Session, dry_run: bool = False) -> dict:
    """Delete S3 pattern folders that have no matching Pattern record in the DB."""
    from app.models.pattern import Pattern

    try:
        from app.services import s3_storage
        from app.services.s3_storage import _get_s3_config
        creds = _get_s3_config()
        import boto3
        s3 = boto3.client("s3",
            region_name=creds.get("region", "us-east-1"),
            aws_access_key_id=creds.get("access_key_id"),
            aws_secret_access_key=creds.get("secret_access_key"))
        bucket = s3_storage._bucket()
    except Exception as e:
        return {"error": f"S3 not configured: {e}"}

    active_ids = {p.id for p in db.query(Pattern).all()}
    resp = s3.list_objects_v2(Bucket=bucket, Prefix="patterns/", Delimiter="/")
    orphan_prefixes = []
    for cp in resp.get("CommonPrefixes", []):
        prefix = cp["Prefix"]
        pattern_id = prefix.strip("/").split("/")[-1]
        if pattern_id not in active_ids:
            orphan_prefixes.append(prefix)

    deleted = 0
    deleted_bytes = 0
    for prefix in orphan_prefixes:
        objects = s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])
        if objects and not dry_run:
            deleted_bytes += sum(o["Size"] for o in objects)
            s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": o["Key"]} for o in objects]})
            deleted += len(objects)
            log.info("S3 GC: deleted %d objects from orphan pattern %s", len(objects), prefix)

    aborted = 0
    try:
        mp_resp = s3.list_multipart_uploads(Bucket=bucket, Prefix="patterns/")
        for upload in mp_resp.get("Uploads", []):
            key_pattern_id = upload["Key"].split("/")[1] if "/" in upload["Key"] else ""
            if key_pattern_id not in active_ids and not dry_run:
                s3.abort_multipart_upload(Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"])
                aborted += 1
    except Exception:
        pass

    result = {"orphan_prefixes": len(orphan_prefixes), "deleted": deleted, "aborted_multipart": aborted}
    if deleted_bytes:
        result["deleted_gb"] = round(deleted_bytes / (1024**3), 1)
    return result


def reconcile_host(host_id: str, dry_run: bool = False) -> dict:
    """Full reconciliation: sync capacity + discover + clean orphans + repair networks."""
    from app.core.database import SessionLocal
    from app.models.host import Host

    db = SessionLocal()
    try:
        host = db.query(Host).filter_by(id=host_id).first()
        if not host:
            return {"error": "Host not found"}

        report = {"host_id": host_id, "host_ip": host.ip_address}

        # Skip GC if any project is deploying on this host
        from app.models.project import Project
        deploying = db.query(Project).filter(
            Project.host_id == host_id,
            Project.state.in_(("deploying", "reconfiguring")),
        ).count()
        if deploying > 0:
            report["skipped"] = f"{deploying} project(s) deploying — skipping GC"
            return report

        report["capacity"] = sync_host_capacity(db, host)

        if not host.ip_address or host.agent_status != "connected":
            report["orphans"] = {"error": "Host not reachable — skipping orphan scan"}
            return report

        orphans = discover_orphans(db, host)
        report["orphans"] = orphans

        if orphans.get("error"):
            return report

        total_orphans = (
            len(orphans.get("orphan_dirs", []))
            + len(orphans.get("orphan_domains", []))
            + len(orphans.get("orphan_bridges", []))
            + len(orphans.get("orphan_namespaces", []))
            + len(orphans.get("orphaned_bmc_project_ids", []))
        )
        report["orphans_found"] = total_orphans
        report["cache_items_found"] = len(orphans.get("cache_items", []))

        cache_count = len(orphans.get("cache_items", []))
        if (total_orphans > 0 or cache_count > 0) and not dry_run:
            cleanup = clean_orphans(host, orphans)
            report["cleanup"] = cleanup
            log.info("Host %s GC: cleaned %d orphans, %d cache items", host_id[:8], cleanup["cleaned"], cache_count)
        elif total_orphans > 0 or cache_count > 0:
            report["cleanup"] = {"dry_run": True, "would_clean": total_orphans + cache_count}
        else:
            report["cleanup"] = {"cleaned": 0}
            log.info("Host %s GC: no orphans found", host_id[:8])

        if not dry_run:
            network_repair = repair_networks(db, host)
            report["network_repair"] = network_repair
            if network_repair.get("repaired", 0) > 0:
                log.info("Host %s GC: repaired %d bridges", host_id[:8], network_repair["repaired"])

        s3_cleanup = clean_s3_orphan_patterns(db, dry_run)
        if s3_cleanup.get("deleted", 0) > 0 or s3_cleanup.get("aborted_multipart", 0) > 0:
            report["s3_cleanup"] = s3_cleanup

        return report

    except Exception as e:
        log.exception("GC failed for host %s: %s", host_id[:8], e)
        return {"error": str(e)}
    finally:
        db.close()


def reconcile_pool(pool_id: str, dry_run: bool = False) -> dict:
    """Pool-level GC for shared storage. Uses any connected host in the pool to scan the filesystem."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project
    from app.models.storage_pool import SharedCacheEntry, StoragePool

    db = SessionLocal()
    try:
        pool = db.query(StoragePool).get(pool_id)
        if not pool:
            return {"error": "Pool not found"}
        if pool.mode == "local":
            return {"error": "Pool-level GC only applies to shared storage pools"}

        report = {"pool_id": pool_id, "pool_name": pool.name, "mode": pool.mode}

        # Find a connected host to run filesystem scans
        scan_host = db.query(Host).filter(
            Host.storage_pool_id == pool_id,
            Host.state == "active",
            Host.agent_status == "connected",
        ).first()
        if not scan_host:
            report["error"] = "No connected host available in pool"
            return report

        # 1. Capacity sync — report shared storage usage
        from app.services.troshkad_client import check_disk_usage
        usage = check_disk_usage(scan_host)
        report["shared_storage"] = usage
        log.info("Pool GC %s: shared storage usage: %s", pool_id[:8], usage)

        # 2. Sync capacity for all hosts in pool
        hosts_in_pool = db.query(Host).filter(
            Host.storage_pool_id == pool_id,
            Host.state == "active",
        ).all()
        for h in hosts_in_pool:
            sync_host_capacity(db, h)
        report["hosts_synced"] = len(hosts_in_pool)

        # 3. Cache eviction — find stale SharedCacheEntries
        from datetime import datetime, timedelta, timezone
        stale_hours = 168  # 7 days
        cutoff = datetime.now(timezone.utc) - timedelta(hours=stale_hours)

        # Get all project IDs in the pool
        pool_host_ids = [h.id for h in hosts_in_pool]
        pool_projects = db.query(Project).filter(
            Project.host_id.in_(pool_host_ids),
            Project.state.in_(["active", "stopped"]),
        ).all()

        # Collect all item IDs referenced by active projects
        referenced_items = set()
        for p in pool_projects:
            topo = p.deployed_topology or p.topology or {}
            for node in topo.get("nodes", []):
                if node.get("type") == "storageNode":
                    data = node.get("data", {})
                    lib_id = data.get("libraryItemId")
                    if lib_id:
                        referenced_items.add(lib_id)
                    pattern_disk_id = data.get("patternDiskId")
                    if pattern_disk_id:
                        referenced_items.add(pattern_disk_id)
                if node.get("type") == "vmNode":
                    pxe_id = node.get("data", {}).get("pxeBootIsoId")
                    if pxe_id:
                        referenced_items.add(pxe_id)

        # Find stale entries not referenced by any project
        stale_entries = db.query(SharedCacheEntry).filter(
            SharedCacheEntry.storage_pool_id == pool_id,
            SharedCacheEntry.status == "ready",
            SharedCacheEntry.created_at < cutoff,
        ).all()

        evictable = [e for e in stale_entries if e.item_id not in referenced_items]
        report["cache_entries_total"] = db.query(SharedCacheEntry).filter(
            SharedCacheEntry.storage_pool_id == pool_id,
        ).count()
        report["cache_entries_stale"] = len(stale_entries)
        report["cache_entries_evictable"] = len(evictable)

        if evictable and not dry_run:
            from app.services.troshkad_client import start_job, wait_for_job
            for entry in evictable:
                full_path = f"/var/lib/troshka/shared/{entry.file_path}"
                try:
                    job_id = start_job(scan_host, "/gc/clean", {"files": [full_path]})
                    wait_for_job(scan_host, job_id, timeout=30)
                except Exception as e:
                    log.warning("Pool GC %s: failed to evict %s: %s", pool_id[:8], entry.file_path, e)
                    continue
                db.delete(entry)
                log.info("Pool GC %s: evicted stale cache entry %s", pool_id[:8], entry.file_path)
            db.commit()
            report["cache_entries_evicted"] = len(evictable)
        elif evictable:
            report["cache_entries_evicted"] = 0
            report["dry_run"] = True

        # 4. Network repair — per host (networks are host-local)
        if not dry_run:
            for h in hosts_in_pool:
                if h.agent_status == "connected":
                    repair_networks(db, h)

        # 5. Orphan cleanup — discover orphans on shared storage
        if scan_host.agent_status == "connected":
            orphans = discover_orphans(db, scan_host)
            report["orphans"] = orphans

            total_orphans = (
                len(orphans.get("orphaned_projects", []))
                + len(orphans.get("orphaned_domains", []))
                + len(orphans.get("orphaned_bridges", []))
                + len(orphans.get("orphaned_namespaces", []))
                + len(orphans.get("orphaned_cache", []))
                + len(orphans.get("stale_cache", []))
            )
            report["orphans_found"] = total_orphans

            if total_orphans > 0 and not dry_run:
                cleanup = clean_orphans(scan_host, orphans)
                report["cleanup"] = cleanup
            elif total_orphans > 0:
                report["cleanup"] = {"dry_run": True, "would_clean": total_orphans}

        log.info("Pool GC %s: complete — %s", pool_id[:8], report)
        return report

    except Exception as e:
        log.exception("Pool GC failed for %s: %s", pool_id[:8], e)
        return {"error": str(e)}
    finally:
        db.close()
