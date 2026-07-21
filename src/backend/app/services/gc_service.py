"""Host garbage collector — reconcile DB state with host reality."""

import logging
from datetime import UTC
from typing import Any

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def sync_host_capacity(db: Session, host) -> dict:
    """Recalculate used_vcpus and used_ram_mb from active projects."""
    from app.models.project import Project

    old = {"used_vcpus": host.used_vcpus, "used_ram_mb": host.used_ram_mb}

    total_vcpus = 0
    total_ram_mb = 0
    for p in (
        db.query(Project)
        .filter(
            Project.host_id == host.id,
            Project.state.in_(["active", "stopped"]),
        )
        .all()
    ):
        topo = p.deployed_topology or p.topology or {}
        for n in topo.get("nodes", []):
            if n.get("type") == "vmNode":
                d = n.get("data", {})
                total_vcpus += d.get("vcpus", 0)
                total_ram_mb += d.get("ram", 0) * 1024
            elif n.get("type") == "containerNode":
                d = n.get("data", {})
                total_vcpus += d.get("cpus", 0)
                total_ram_mb += d.get("memory", 0)

    host.used_vcpus = total_vcpus
    host.used_ram_mb = total_ram_mb
    db.commit()

    new = {"used_vcpus": total_vcpus, "used_ram_mb": total_ram_mb}
    changed = old != new
    if changed:
        log.info("Host %s capacity synced: %s -> %s", host.id[:8], old, new)

    return {"old": old, "new": new, "changed": changed}


def _clean_orphaned_routes(db, driver, provider, report):
    """Find and delete OCP Routes/Services for projects that no longer exist."""
    from typing import Any, cast

    from app.models.project import Project

    creds = provider.get_credentials()
    namespace = creds.get("namespace", "troshka")

    try:
        from app.services.providers.ocpvirt import _get_k8s_clients

        custom_api, core_api = _get_k8s_clients(creds)
    except Exception:
        return

    label_selector = "troshka/access-type=route"
    try:
        svcs = cast(
            Any,
            core_api.list_namespaced_service(namespace, label_selector=label_selector),
        )
    except Exception:
        return

    active_project_prefixes = {
        p.id[:8]
        for p in db.query(Project).filter(
            Project.state.in_(("active", "stopped", "deploying", "draft"))
        )
    }

    orphaned = 0
    for svc in svcs.items:
        pid = svc.metadata.labels.get("troshka/project-id", "")
        if pid and pid not in active_project_prefixes:
            try:
                core_api.delete_namespaced_service(svc.metadata.name, namespace)
                orphaned += 1
            except Exception:
                pass
            try:
                custom_api.delete_namespaced_custom_object(
                    group="route.openshift.io",
                    version="v1",
                    namespace=namespace,
                    plural="routes",
                    name=svc.metadata.name,
                )
            except Exception:
                pass

    if orphaned:
        report["routes_cleaned"] = orphaned
        log.info("GC: cleaned %d orphaned Route access resources", orphaned)


def discover_orphans(db: Session, host) -> dict:
    """Discover orphaned resources on host via troshkad."""
    from app.models.project import Project
    from app.services.troshkad_client import start_job, wait_for_job

    if not host.ip_address or host.agent_status != "connected":
        return {
            "error": "Host not reachable",
            "orphaned_projects": [],
            "orphaned_domains": [],
            "orphaned_bridges": [],
        }

    # Build list of known project IDs and domains for GC
    # Include ALL projects in the same pool (shared storage is visible to all hosts)
    active_project_ids = []
    known_domains = []
    skip_states = {"deploying", "reconfiguring"}

    from app.models.host import Host as HostModel

    pool_host_ids = [host.id]
    if host.storage_pool_id:
        pool_host_ids = [
            h.id
            for h in db.query(HostModel)
            .filter(HostModel.storage_pool_id == host.storage_pool_id)
            .all()
        ]

    for p in db.query(Project).filter(Project.host_id.in_(pool_host_ids)).all():
        if p.state in skip_states or p.state in ("active", "stopped"):
            active_project_ids.append(p.id)
            pid_short = p.id[:8]
            known_domains.append(f"troshka-{pid_short}")

    # Build list of project IDs that should have BMC
    bmc_project_ids = set()
    for p in db.query(Project).filter(Project.host_id.in_(pool_host_ids)).all():
        if p.state in ("active", "stopped"):
            topo = p.deployed_topology or p.topology or {}
            for node in topo.get("nodes", []):
                if (
                    node.get("type") == "networkNode"
                    and node.get("data", {}).get("networkType") == "bmc"
                ):
                    bmc_project_ids.add(p.id)
                    break

    # Call troshkad to discover orphans
    job_id = start_job(
        host,
        "/gc/discover",
        {
            "known_project_ids": active_project_ids,
            "known_domains": known_domains,
            "known_bmc_project_ids": list(bmc_project_ids),
        },
    )
    job = wait_for_job(host, job_id, timeout=30)
    if job["status"] == "failed":
        return {"error": job["result"].get("error", "Discovery failed")}

    return job["result"]


def _find_orphaned_cache(db: Session, cache_items: list[dict]) -> list[str]:
    """Filter cache items to those with no matching DB record."""
    from app.models.library import LibraryItem
    from app.models.pattern import Pattern

    active_pattern_ids = {p.id for p in db.query(Pattern).all()}
    active_image_ids = {i.id for i in db.query(LibraryItem).all()}
    active_ids = active_pattern_ids | active_image_ids

    orphaned = []
    for item in cache_items:
        path = item.get("path", "") if isinstance(item, dict) else str(item)
        entry_name = path.rstrip("/").rsplit("/", 1)[-1]
        entry_id = entry_name.rsplit(".", 1)[0]
        if entry_id not in active_ids:
            orphaned.append(path)
    return orphaned


def clean_orphans(host, orphans: dict, db: Session | None = None) -> dict:
    """Clean orphaned resources on host via troshkad."""
    from app.services.troshkad_client import start_job, wait_for_job

    if not host.ip_address or host.agent_status != "connected":
        return {"error": "Host not reachable", "cleaned": 0}

    cache_items = []
    if db:
        cache_items = _find_orphaned_cache(db, orphans.get("cache_items", []))
    cache_items.extend(orphans.get("stale_temps", []))

    job_id = start_job(
        host,
        "/gc/clean",
        {
            "orphan_dirs": list(set(orphans.get("orphan_dirs", []))),
            "orphan_domains": list(set(orphans.get("orphan_domains", []))),
            "orphan_containers": orphans.get("orphan_containers", []),
            "orphan_bridges": orphans.get("orphan_bridges", []),
            "orphan_namespaces": orphans.get("orphan_namespaces", []),
            "cache_items": cache_items,
            "orphan_bmc_project_ids": orphans.get("orphaned_bmc_project_ids", []),
            "orphan_metadata_ids": orphans.get("orphaned_metadata_ids", []),
        },
    )
    job = wait_for_job(host, job_id, timeout=120)

    cleaned = (
        len(orphans.get("orphan_dirs", []))
        + len(orphans.get("orphan_domains", []))
        + len(orphans.get("orphan_containers", []))
        + len(orphans.get("orphan_bridges", []))
        + len(orphans.get("orphan_namespaces", []))
        + len(orphans.get("orphaned_bmc_project_ids", []))
        + len(cache_items)
    )
    return {
        "success": job["status"] == "completed",
        "cleaned": cleaned,
        "cache_cleaned": len(cache_items),
        "output": "\n".join(job.get("output", [])),
    }


def repair_networks(db: Session, host) -> dict:
    """Ensure VXLAN bridges exist for all active/stopped projects on this host."""
    from app.models.project import Project
    from app.services.deploy_service import _setup_networks_via_troshkad
    from app.services.troshkad_client import TroshkadError, start_job, wait_for_job

    if not host.ip_address or host.agent_status != "connected":
        return {"repaired": 0, "error": "Host not reachable"}

    projects = (
        db.query(Project)
        .filter(
            Project.host_id == host.id,
            Project.state.in_(["active", "stopped"]),
        )
        .all()
    )

    if not projects:
        return {"repaired": 0}

    # Check which bridges already exist on the host
    existing_bridges = set()
    try:
        job_id = start_job(host, "/networks/list-bridges", {})
        job = wait_for_job(host, job_id, timeout=15)
        if job["status"] == "completed":
            existing_bridges = set(job.get("result", {}).get("bridges", []))
    except TroshkadError:
        pass

    repaired = 0
    for p in projects:
        project_vnis = set(str(v) for v in (p.vni_map or {}).values())
        if not project_vnis:
            continue
        missing = [v for v in project_vnis if f"br-{v}" not in existing_bridges]
        if not missing:
            continue
        topo = p.deployed_topology or p.topology or {}
        result = _setup_networks_via_troshkad(host, topo, p.vni_map or {}, db, p.id)
        if result is True:
            repaired += len(missing)
            log.info("Repaired %d bridges for project %s", len(missing), p.id[:8])
        else:
            log.warning("Failed to repair bridges for project %s: %s", p.id[:8], result)

    return {"repaired": repaired}


_recovering_hosts: set[str] = set()


def recover_host_services(host_id: str):
    """Restore networking and BMC for all active projects after a host restart.

    Triggered by the health poller when a host transitions from disconnected to
    connected.  Runs in a background thread.  Safe to call concurrently — a
    per-host guard prevents duplicate recovery.
    """
    if host_id in _recovering_hosts:
        log.debug("Recovery already running for host %s, skipping", host_id[:8])
        return
    _recovering_hosts.add(host_id)

    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.project import Project
    from app.services.deploy_service import (
        _extract_bmc_config,
        _setup_bmc_via_troshkad,
    )
    from app.services.troshkad_client import get_all_vm_states, start_job, wait_for_job

    db = SessionLocal()
    try:
        host = db.query(Host).filter_by(id=host_id).first()
        if not host or host.agent_status != "connected":
            return

        projects = (
            db.query(Project)
            .filter(
                Project.host_id == host_id,
                Project.state.in_(["active", "stopped"]),
            )
            .all()
        )
        if not projects:
            return

        busy = any(p.state in ("deploying", "reconfiguring") for p in projects)
        if busy:
            log.info("Host %s has busy projects, deferring recovery", host_id[:8])
            return

        log.info(
            "Host %s reconnected — recovering %d project(s)", host_id[:8], len(projects)
        )

        net_result = repair_networks(db, host)
        log.info("Host %s network repair: %s", host_id[:8], net_result)

        # Reconnect running VMs' TAPs to restored namespace bridges
        from app.services.troshkad_client import get_all_vm_states

        vm_states = get_all_vm_states(host) or {}
        for p in projects:
            ns_prefix = f"troshka-{p.id[:8]}-"
            running_domains = [
                d
                for d, s in vm_states.items()
                if s == "running" and d.startswith(ns_prefix)
            ]
            if not running_domains:
                continue
            try:
                tap_job = start_job(
                    host,
                    "/networks/reconnect-taps",
                    {"project_id": p.id, "domains": running_domains},
                )
                tap_result = wait_for_job(host, tap_job, timeout=30)
                rc = tap_result.get("result", {}).get("reconnected", 0)
                if rc:
                    log.info("Reconnected %d TAPs for project %s", rc, p.id[:8])
            except Exception:
                log.warning("TAP reconnect failed for project %s (non-fatal)", p.id[:8])

        bmc_restored = 0
        for p in projects:
            topo = p.deployed_topology or p.topology or {}
            bmc_config = _extract_bmc_config(topo, p.id)
            if not bmc_config:
                continue
            try:
                _setup_bmc_via_troshkad(host, p.id, bmc_config)
                bmc_restored += 1
                log.info("Restored BMC for project %s", p.id[:8])
            except Exception:
                log.warning("BMC restore failed for project %s (non-fatal)", p.id[:8])

        log.info(
            "Host %s recovery complete: %d networks, %d BMC projects",
            host_id[:8],
            net_result.get("repaired", 0),
            bmc_restored,
        )
    except Exception:
        log.exception("Host %s recovery failed", host_id[:8])
    finally:
        db.close()
        _recovering_hosts.discard(host_id)


def clean_s3_orphans(db: Session, dry_run: bool = False) -> dict:
    """Delete S3 objects that have no matching DB record (patterns, snapshots, library items)."""
    from app.models.library import LibraryItem
    from app.models.pattern import Pattern

    try:
        from app.services import s3_storage
        from app.services.s3_storage import _get_s3_config

        creds = _get_s3_config()
        import boto3

        s3 = boto3.client(
            "s3",
            region_name=creds.get("region", "us-east-1"),
            aws_access_key_id=creds.get("access_key_id"),
            aws_secret_access_key=creds.get("secret_access_key"),
        )
        bucket = s3_storage._bucket()
    except Exception as e:
        return {"error": f"S3 not configured: {e}"}

    active_pattern_ids = {p.id for p in db.query(Pattern).all()}
    active_library_ids = {i.id for i in db.query(LibraryItem).all()}

    deleted = 0
    deleted_bytes = 0

    # Scan each S3 prefix type for orphans
    # patterns/ and snapshots/ are: {prefix}/{item_id}/...
    # library/ is: library/{user_id}/{item_id}/... (extra nesting level)
    for s3_prefix, active_ids in [
        ("patterns/", active_pattern_ids),
        ("snapshots/", active_library_ids),
    ]:
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=s3_prefix, Delimiter="/")
        for cp in resp.get("CommonPrefixes", []):
            prefix = cp["Prefix"]
            item_id = prefix.strip("/").split("/")[-1]
            if item_id not in active_ids:
                objects = s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get(
                    "Contents", []
                )
                if objects and not dry_run:
                    deleted_bytes += sum(o["Size"] for o in objects)
                    s3.delete_objects(
                        Bucket=bucket,
                        Delete={"Objects": [{"Key": o["Key"]} for o in objects]},
                    )
                    deleted += len(objects)
                    log.info(
                        "S3 GC: deleted %d objects from orphan %s", len(objects), prefix
                    )

    # library/ has extra nesting: library/{user_id}/{item_id}/...
    # Must scan two levels deep to find the item_id
    resp = s3.list_objects_v2(Bucket=bucket, Prefix="library/", Delimiter="/")
    for user_cp in resp.get("CommonPrefixes", []):
        user_prefix = user_cp["Prefix"]
        items_resp = s3.list_objects_v2(
            Bucket=bucket, Prefix=user_prefix, Delimiter="/"
        )
        for item_cp in items_resp.get("CommonPrefixes", []):
            item_prefix = item_cp["Prefix"]
            item_id = item_prefix.strip("/").split("/")[-1]
            if item_id not in active_library_ids:
                objects = s3.list_objects_v2(Bucket=bucket, Prefix=item_prefix).get(
                    "Contents", []
                )
                if objects and not dry_run:
                    deleted_bytes += sum(o["Size"] for o in objects)
                    s3.delete_objects(
                        Bucket=bucket,
                        Delete={"Objects": [{"Key": o["Key"]} for o in objects]},
                    )
                    deleted += len(objects)
                    log.info(
                        "S3 GC: deleted %d objects from orphan library item %s",
                        len(objects),
                        item_prefix,
                    )

    # Abort stale multipart uploads
    aborted = 0
    all_active = active_pattern_ids | active_library_ids
    try:
        mp_resp = s3.list_multipart_uploads(Bucket=bucket)
        for upload in mp_resp.get("Uploads", []):
            parts = upload["Key"].split("/")
            item_id = parts[1] if len(parts) > 1 else ""
            if item_id and item_id not in all_active and not dry_run:
                s3.abort_multipart_upload(
                    Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"]
                )
                aborted += 1
    except Exception:
        pass

    result = {"deleted": deleted, "aborted_multipart": aborted}
    if deleted_bytes:
        result["deleted_gb"] = round(deleted_bytes / (1024**3), 1)  # type: ignore[assignment]
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

        report: dict[str, Any] = {"host_id": host_id, "host_ip": host.ip_address}

        # Skip GC if any project is deploying on this host
        from app.models.project import Project

        deploying = (
            db.query(Project)
            .filter(
                Project.host_id == host_id,
                Project.state.in_(("deploying", "reconfiguring")),
            )
            .count()
        )
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
            + len(orphans.get("orphan_containers", []))
            + len(orphans.get("orphan_bridges", []))
            + len(orphans.get("orphan_namespaces", []))
            + len(orphans.get("orphaned_bmc_project_ids", []))
        )
        report["orphans_found"] = total_orphans
        orphaned_cache = _find_orphaned_cache(db, orphans.get("cache_items", []))
        stale_temps = orphans.get("stale_temps", [])
        report["cache_orphaned"] = len(orphaned_cache)
        report["stale_temps_found"] = len(stale_temps)

        cleanable = total_orphans + len(orphaned_cache) + len(stale_temps)
        if cleanable > 0 and not dry_run:
            cleanup = clean_orphans(host, orphans, db)
            report["cleanup"] = cleanup
            log.info(
                "Host %s GC: cleaned %d orphans (%d cache)",
                host_id[:8],
                cleanup["cleaned"],
                cleanup.get("cache_cleaned", 0),
            )
        elif cleanable > 0:
            report["cleanup"] = {"dry_run": True, "would_clean": cleanable}
        else:
            report["cleanup"] = {"cleaned": 0}
            log.info("Host %s GC: no orphans found", host_id[:8])

        if not dry_run:
            network_repair = repair_networks(db, host)
            report["network_repair"] = network_repair
            if network_repair.get("repaired", 0) > 0:
                log.info(
                    "Host %s GC: repaired %d bridges",
                    host_id[:8],
                    network_repair["repaired"],
                )

        s3_cleanup = clean_s3_orphans(db, dry_run)
        if (
            s3_cleanup.get("deleted", 0) > 0
            or s3_cleanup.get("aborted_multipart", 0) > 0
        ):
            report["s3_cleanup"] = s3_cleanup

        # Clean orphaned OCP Routes/Services (OCP Virt only)
        if not dry_run and host.provider_id:
            from app.models.provider import Provider

            provider = db.query(Provider).filter_by(id=host.provider_id).first()
            if provider and provider.type == "ocpvirt":
                try:
                    from app.services.providers import get_provider_driver

                    driver = get_provider_driver(provider)
                    _clean_orphaned_routes(db, driver, provider, report)
                except Exception:
                    log.warning(
                        "Host %s GC: Route cleanup failed (non-fatal)",
                        host_id[:8],
                        exc_info=True,
                    )

        # Re-sync capacity after cleanup freed disk space
        if not dry_run and report.get("cleanup", {}).get("cache_cleaned", 0) > 0:
            report["capacity_after"] = sync_host_capacity(db, host)

        # Clean orphaned SharedCacheEntries
        if not dry_run and host.storage_pool_id:
            from app.models.library import LibraryItem
            from app.models.pattern import Pattern
            from app.models.storage_pool import SharedCacheEntry

            active_ids = {p.id for p in db.query(Pattern).all()} | {
                i.id for i in db.query(LibraryItem).all()
            }
            orphaned_entries = (
                db.query(SharedCacheEntry)
                .filter(
                    SharedCacheEntry.storage_pool_id == host.storage_pool_id,
                    ~SharedCacheEntry.item_id.in_(active_ids),
                )
                .all()
            )
            if orphaned_entries:
                for entry in orphaned_entries:
                    db.delete(entry)
                db.commit()
                report["shared_cache_entries_cleaned"] = len(orphaned_entries)
                log.info(
                    "Host %s GC: cleaned %d orphaned SharedCacheEntries",
                    host_id[:8],
                    len(orphaned_entries),
                )

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
        pool = db.get(StoragePool, pool_id)
        if not pool:
            return {"error": "Pool not found"}
        if pool.mode == "local":
            return {"error": "Pool-level GC only applies to shared storage pools"}

        report: dict[str, Any] = {
            "pool_id": pool_id,
            "pool_name": pool.name,
            "mode": pool.mode,
        }

        # Find a connected host to run filesystem scans
        scan_host = (
            db.query(Host)
            .filter(
                Host.storage_pool_id == pool_id,
                Host.state == "active",
                Host.agent_status == "connected",
            )
            .first()
        )
        if not scan_host:
            report["error"] = "No connected host available in pool"
            return report

        # 1. Capacity sync — report shared storage usage
        from app.services.troshkad_client import check_disk_usage

        usage = check_disk_usage(scan_host)
        report["shared_storage"] = usage
        log.info("Pool GC %s: shared storage usage: %s", pool_id[:8], usage)

        # 2. Sync capacity for all hosts in pool
        hosts_in_pool = (
            db.query(Host)
            .filter(
                Host.storage_pool_id == pool_id,
                Host.state == "active",
            )
            .all()
        )
        for h in hosts_in_pool:
            sync_host_capacity(db, h)
        report["hosts_synced"] = len(hosts_in_pool)

        # 3. Cache eviction — find stale SharedCacheEntries
        from datetime import datetime, timedelta

        stale_hours = 168  # 7 days
        cutoff = datetime.now(UTC) - timedelta(hours=stale_hours)

        # Get all project IDs in the pool
        pool_host_ids = [h.id for h in hosts_in_pool]
        pool_projects = (
            db.query(Project)
            .filter(
                Project.host_id.in_(pool_host_ids),
                Project.state.in_(["active", "stopped"]),
            )
            .all()
        )

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
        stale_entries = (
            db.query(SharedCacheEntry)
            .filter(
                SharedCacheEntry.storage_pool_id == pool_id,
                SharedCacheEntry.status == "ready",
                SharedCacheEntry.created_at < cutoff,
            )
            .all()
        )

        evictable = [e for e in stale_entries if e.item_id not in referenced_items]
        report["cache_entries_total"] = (
            db.query(SharedCacheEntry)
            .filter(
                SharedCacheEntry.storage_pool_id == pool_id,
            )
            .count()
        )
        report["cache_entries_stale"] = len(stale_entries)
        report["cache_entries_evictable"] = len(evictable)

        if evictable and not dry_run:
            from app.services.troshkad_client import start_job, wait_for_job

            for entry in evictable:
                full_path = f"/var/lib/troshka/shared/{entry.file_path}"
                try:
                    job_id = start_job(
                        scan_host, "/gc/clean", {"cache_items": [full_path]}
                    )
                    wait_for_job(scan_host, job_id, timeout=30)
                except Exception as e:
                    log.warning(
                        "Pool GC %s: failed to evict %s: %s",
                        pool_id[:8],
                        entry.file_path,
                        e,
                    )
                    continue
                db.delete(entry)
                log.info(
                    "Pool GC %s: evicted stale cache entry %s",
                    pool_id[:8],
                    entry.file_path,
                )
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
                len(orphans.get("orphan_dirs", []))
                + len(orphans.get("orphan_domains", []))
                + len(orphans.get("orphan_containers", []))
                + len(orphans.get("orphan_bridges", []))
                + len(orphans.get("orphan_namespaces", []))
                + len(orphans.get("orphaned_bmc_project_ids", []))
            )
            cache_count = len(orphans.get("cache_items", []))
            stale_count = len(orphans.get("stale_temps", []))
            report["orphans_found"] = total_orphans

            if (
                total_orphans > 0 or cache_count > 0 or stale_count > 0
            ) and not dry_run:
                cleanup = clean_orphans(scan_host, orphans, db)
                report["cleanup"] = cleanup
            elif total_orphans > 0 or cache_count > 0 or stale_count > 0:
                report["cleanup"] = {
                    "dry_run": True,
                    "would_clean": total_orphans + cache_count + stale_count,
                }

        log.info("Pool GC %s: complete — %s", pool_id[:8], report)
        return report

    except Exception as e:
        log.exception("Pool GC failed for %s: %s", pool_id[:8], e)
        return {"error": str(e)}
    finally:
        db.close()
