"""Host garbage collector — reconcile DB state with host reality."""

import logging
from datetime import UTC
from typing import Any

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

_HOST_NOT_REACHABLE = "Host not reachable"


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


def _clean_orphaned_routes(db, _driver, provider, report):
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


def _get_pool_host_ids(db: Session, host) -> list[str]:
    """Get host IDs for the host's storage pool (or just the host itself)."""
    if not host.storage_pool_id:
        return [host.id]
    from app.models.host import Host as HostModel

    return [
        h.id
        for h in db.query(HostModel)
        .filter(HostModel.storage_pool_id == host.storage_pool_id)
        .all()
    ]


def _collect_known_projects_and_domains(db: Session, pool_host_ids):
    """Collect known project IDs and domain prefixes for GC filtering."""
    from app.models.project import Project

    active_project_ids = []
    known_domains = []
    skip_states = {"deploying", "reconfiguring"}

    for p in db.query(Project).filter(Project.host_id.in_(pool_host_ids)).all():
        if p.state in skip_states or p.state in ("active", "stopped"):
            active_project_ids.append(p.id)
            known_domains.append(f"troshka-{p.id[:8]}")
    return active_project_ids, known_domains


def _collect_bmc_project_ids(db: Session, pool_host_ids) -> list[str]:
    """Collect project IDs that have BMC network nodes."""
    from app.models.project import Project

    bmc_project_ids = set()
    for p in db.query(Project).filter(Project.host_id.in_(pool_host_ids)).all():
        if p.state not in ("active", "stopped"):
            continue
        topo = p.deployed_topology or p.topology or {}
        for node in topo.get("nodes", []):
            if (
                node.get("type") == "networkNode"
                and node.get("data", {}).get("networkType") == "bmc"
            ):
                bmc_project_ids.add(p.id)
                break
    return list(bmc_project_ids)


def discover_orphans(db: Session, host) -> dict:
    """Discover orphaned resources on host via troshkad."""
    from app.services.troshkad_client import start_job, wait_for_job

    if not host.ip_address or host.agent_status != "connected":
        return {
            "error": _HOST_NOT_REACHABLE,
            "orphaned_projects": [],
            "orphaned_domains": [],
            "orphaned_bridges": [],
        }

    pool_host_ids = _get_pool_host_ids(db, host)
    active_project_ids, known_domains = _collect_known_projects_and_domains(
        db,
        pool_host_ids,
    )
    bmc_project_ids = _collect_bmc_project_ids(db, pool_host_ids)

    job_id = start_job(
        host,
        "/gc/discover",
        {
            "known_project_ids": active_project_ids,
            "known_domains": known_domains,
            "known_bmc_project_ids": bmc_project_ids,
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
        return {"error": _HOST_NOT_REACHABLE, "cleaned": 0}

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


def _get_existing_bridges(host) -> set[str]:
    """Get the set of bridge names that currently exist on the host."""
    from app.services.troshkad_client import TroshkadError, start_job, wait_for_job

    try:
        job_id = start_job(host, "/networks/list-bridges", {})
        job = wait_for_job(host, job_id, timeout=15)
        if job["status"] == "completed":
            return set(job.get("result", {}).get("bridges", []))
    except TroshkadError:
        pass
    return set()


def repair_networks(db: Session, host) -> dict:
    """Ensure VXLAN bridges exist for all active/stopped projects on this host."""
    from app.models.project import Project
    from app.services.deploy_service import _setup_networks_via_troshkad

    if not host.ip_address or host.agent_status != "connected":
        return {"repaired": 0, "error": _HOST_NOT_REACHABLE}

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

    existing_bridges = _get_existing_bridges(host)

    repaired = 0
    for p in projects:
        project_vnis = {str(v) for v in (p.vni_map or {}).values()}
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


def _recover_mesh_peers(db, host, host_id: str, mesh_peers) -> None:
    """Recover WireGuard mesh interfaces for all mesh peers on a host."""
    from app.services.mesh_service import get_peer_config_for_host
    from app.services.troshkad_client import start_job, wait_for_job

    for peer in mesh_peers:
        try:
            config = get_peer_config_for_host(db, peer.project_id, host_id)
            job_id = start_job(host, "/mesh/setup", config)
            wait_for_job(host, job_id, timeout=60)
            log.info(
                "Recovered mesh for project %s on host %s",
                peer.project_id[:8],
                host_id[:8],
            )
        except Exception as e:
            log.warning("Failed to recover mesh for %s: %s", peer.project_id[:8], e)


def _reconnect_project_taps(host, projects, vm_states: dict) -> None:
    """Reconnect running VMs' TAPs to restored namespace bridges."""
    from app.services.troshkad_client import start_job, wait_for_job

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


def _restore_project_bmc(host, projects) -> int:
    """Restore BMC services for projects with BMC configuration.

    Returns the number of projects whose BMC was successfully restored.
    """
    from app.services.deploy_service import _setup_bmc_via_troshkad
    from app.services.deploy_topology import _extract_bmc_config

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
    return bmc_restored


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
    from app.models.mesh_peer import ProjectMeshPeer
    from app.models.project import Project
    from app.services.troshkad_client import get_all_vm_states

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

        mesh_peers = db.query(ProjectMeshPeer).filter_by(host_id=host_id).all()
        _recover_mesh_peers(db, host, host_id, mesh_peers)

        net_result = repair_networks(db, host)
        log.info("Host %s network repair: %s", host_id[:8], net_result)

        vm_states = get_all_vm_states(host) or {}
        _reconnect_project_taps(host, projects, vm_states)

        bmc_restored = _restore_project_bmc(host, projects)

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


def _delete_s3_prefix_objects(s3, paginator, bucket, prefix, op):
    """List and delete all objects under an S3 prefix. Returns (count, bytes)."""
    objects = []
    for obj_page in paginator.paginate(Bucket=bucket, Prefix=prefix, **op):
        objects.extend(obj_page.get("Contents", []))
    if not objects:
        return 0, 0
    total_bytes = sum(o["Size"] for o in objects)
    s3.delete_objects(
        Bucket=bucket,
        Delete={"Objects": [{"Key": o["Key"]} for o in objects]},
        **op,
    )
    return len(objects), total_bytes


def _scan_flat_prefix_orphans(
    s3, paginator, bucket, op, s3_prefix, active_ids, dry_run
):
    """Scan a flat S3 prefix (patterns/, snapshots/) for orphaned item directories."""
    deleted = 0
    deleted_bytes = 0
    for page in paginator.paginate(
        Bucket=bucket, Prefix=s3_prefix, Delimiter="/", **op
    ):
        for cp in page.get("CommonPrefixes", []):
            prefix = cp["Prefix"]
            item_id = prefix.strip("/").split("/")[-1]
            if item_id in active_ids:
                continue
            if dry_run:
                continue
            count, nbytes = _delete_s3_prefix_objects(
                s3,
                paginator,
                bucket,
                prefix,
                op,
            )
            deleted += count
            deleted_bytes += nbytes
            if count:
                log.info("S3 GC: deleted %d objects from orphan %s", count, prefix)
    return deleted, deleted_bytes


def _scan_library_user_items(
    s3, paginator, bucket, op, user_prefix, active_ids, dry_run
):
    """Scan a single user's library prefix for orphaned item directories."""
    deleted = 0
    deleted_bytes = 0
    for items_page in paginator.paginate(
        Bucket=bucket, Prefix=user_prefix, Delimiter="/", **op
    ):
        for item_cp in items_page.get("CommonPrefixes", []):
            item_prefix = item_cp["Prefix"]
            item_id = item_prefix.strip("/").split("/")[-1]
            if item_id in active_ids:
                continue
            if dry_run:
                continue
            count, nbytes = _delete_s3_prefix_objects(
                s3, paginator, bucket, item_prefix, op
            )
            deleted += count
            deleted_bytes += nbytes
            if count:
                log.info(
                    "S3 GC: deleted %d objects from orphan library item %s",
                    count,
                    item_prefix,
                )
    return deleted, deleted_bytes


def _scan_library_prefix_orphans(s3, paginator, bucket, op, active_ids, dry_run):
    """Scan library/ prefix (two-level nesting) for orphaned item directories."""
    deleted = 0
    deleted_bytes = 0
    for page in paginator.paginate(
        Bucket=bucket, Prefix="library/", Delimiter="/", **op
    ):
        for user_cp in page.get("CommonPrefixes", []):
            d, b = _scan_library_user_items(
                s3, paginator, bucket, op, user_cp["Prefix"], active_ids, dry_run
            )
            deleted += d
            deleted_bytes += b
    return deleted, deleted_bytes


def _abort_stale_multipart_uploads(s3, bucket, op, all_active_ids, dry_run):
    """Abort multipart uploads for items that no longer exist in the DB."""
    aborted = 0
    try:
        mp_resp = s3.list_multipart_uploads(Bucket=bucket, **op)
        for upload in mp_resp.get("Uploads", []):
            parts = upload["Key"].split("/")
            item_id = parts[1] if len(parts) > 1 else ""
            if item_id and item_id not in all_active_ids and not dry_run:
                s3.abort_multipart_upload(
                    Bucket=bucket,
                    Key=upload["Key"],
                    UploadId=upload["UploadId"],
                    **op,
                )
                aborted += 1
    except Exception:
        pass
    return aborted


def clean_s3_orphans(db: Session, dry_run: bool = False) -> dict:
    """Delete S3 objects that have no matching DB record (patterns, snapshots, library items)."""
    from app.models.library import LibraryItem
    from app.models.pattern import Pattern

    try:
        from app.services import s3_storage
        from app.services.s3_storage import _get_s3_config, owner_params

        creds = _get_s3_config()
        import boto3

        s3 = boto3.client(
            "s3",
            region_name=creds.get("region", "us-east-1"),
            aws_access_key_id=creds.get("access_key_id"),
            aws_secret_access_key=creds.get("secret_access_key"),
        )
        bucket = s3_storage._bucket()
        op = owner_params(creds)
    except Exception as e:
        return {"error": f"S3 not configured: {e}"}

    active_pattern_ids = {p.id for p in db.query(Pattern).all()}
    active_library_ids = {i.id for i in db.query(LibraryItem).all()}

    paginator = s3.get_paginator("list_objects_v2")
    deleted = 0
    deleted_bytes = 0

    for s3_prefix, active_ids in [
        ("patterns/", active_pattern_ids),
        ("snapshots/", active_library_ids),
    ]:
        d, b = _scan_flat_prefix_orphans(
            s3,
            paginator,
            bucket,
            op,
            s3_prefix,
            active_ids,
            dry_run,
        )
        deleted += d
        deleted_bytes += b

    d, b = _scan_library_prefix_orphans(
        s3,
        paginator,
        bucket,
        op,
        active_library_ids,
        dry_run,
    )
    deleted += d
    deleted_bytes += b

    all_active = active_pattern_ids | active_library_ids
    aborted = _abort_stale_multipart_uploads(s3, bucket, op, all_active, dry_run)

    result = {"deleted": deleted, "aborted_multipart": aborted}
    if deleted_bytes:
        result["deleted_gb"] = round(deleted_bytes / (1024**3), 1)  # type: ignore[assignment]
    return result


def _count_total_orphans(orphans: dict) -> int:
    """Count the total number of orphaned resources across all categories."""
    return (
        len(orphans.get("orphan_dirs", []))
        + len(orphans.get("orphan_domains", []))
        + len(orphans.get("orphan_containers", []))
        + len(orphans.get("orphan_bridges", []))
        + len(orphans.get("orphan_namespaces", []))
        + len(orphans.get("orphaned_bmc_project_ids", []))
    )


def _reconcile_clean_orphans(db, host, host_id, orphans, dry_run, report):
    """Discover and clean orphaned resources, populate report."""
    total_orphans = _count_total_orphans(orphans)
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


def _reconcile_ocp_routes(db, host, host_id, report):
    """Clean orphaned OCP Routes/Services (OCP Virt only)."""
    if not host.provider_id:
        return
    from app.models.provider import Provider

    provider = db.query(Provider).filter_by(id=host.provider_id).first()
    if not provider or provider.type != "ocpvirt":
        return
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


def _reconcile_shared_cache_entries(db, host, host_id, report):
    """Clean orphaned SharedCacheEntries for the host's storage pool."""
    if not host.storage_pool_id:
        return
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


def _clean_cluster_rgw_orphans(db: Session, dry_run: bool) -> list[dict]:
    """Scan all KubeVirt providers with OBC config and clean orphaned pattern objects."""
    from app.models.provider import Provider
    from app.services import cluster_storage

    providers = (
        db.query(Provider)
        .filter(Provider.type == "kubevirt", Provider.state == "active")
        .all()
    )
    results = []
    for p in providers:
        creds = p.get_credentials()
        if not creds or "s3_config" not in creds:
            continue
        report = cluster_storage.clean_orphans(db, p.id, dry_run)
        if report.get("orphan_patterns", 0) > 0 or report.get("error"):
            results.append(report)
    return results


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

        _reconcile_clean_orphans(db, host, host_id, orphans, dry_run, report)

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

        cluster_rgw_cleanup = _clean_cluster_rgw_orphans(db, dry_run)
        if cluster_rgw_cleanup:
            report["cluster_rgw_cleanup"] = cluster_rgw_cleanup

        if not dry_run:
            _reconcile_ocp_routes(db, host, host_id, report)

        # Re-sync capacity after cleanup freed disk space
        if not dry_run and report.get("cleanup", {}).get("cache_cleaned", 0) > 0:
            report["capacity_after"] = sync_host_capacity(db, host)

        if not dry_run:
            _reconcile_shared_cache_entries(db, host, host_id, report)

        return report

    except Exception as e:
        log.exception("GC failed for host %s: %s", host_id[:8], e)
        return {"error": str(e)}
    finally:
        db.close()


def _extract_node_item_ids(node: dict) -> list[str]:
    """Extract referenced item IDs from a single topology node."""
    ids = []
    node_type = node.get("type")
    if node_type == "storageNode":
        data = node.get("data", {})
        lib_id = data.get("libraryItemId")
        if lib_id:
            ids.append(lib_id)
        pattern_disk_id = data.get("patternDiskId")
        if pattern_disk_id:
            ids.append(pattern_disk_id)
    elif node_type == "vmNode":
        pxe_id = node.get("data", {}).get("pxeBootIsoId")
        if pxe_id:
            ids.append(pxe_id)
    return ids


def _collect_referenced_items(pool_projects) -> set[str]:
    """Collect all item IDs referenced by active projects in a pool."""
    referenced_items = set()
    for p in pool_projects:
        topo = p.deployed_topology or p.topology or {}
        for node in topo.get("nodes", []):
            referenced_items.update(_extract_node_item_ids(node))
    return referenced_items


def _evict_stale_cache_entries(db, pool_id, scan_host, evictable, dry_run, report):
    """Evict stale shared cache entries from disk and DB."""
    if evictable and not dry_run:
        from app.services.troshkad_client import start_job, wait_for_job

        for entry in evictable:
            full_path = f"/var/lib/troshka/shared/{entry.file_path}"
            try:
                job_id = start_job(
                    scan_host,
                    "/gc/clean",
                    {"cache_items": [full_path]},
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


def _pool_cache_eviction(db, pool_id, hosts_in_pool, scan_host, dry_run, report):
    """Find and evict stale SharedCacheEntries in a pool."""
    from datetime import datetime, timedelta

    from app.models.project import Project
    from app.models.storage_pool import SharedCacheEntry

    stale_hours = 168  # 7 days
    cutoff = datetime.now(UTC) - timedelta(hours=stale_hours)

    pool_host_ids = [h.id for h in hosts_in_pool]
    pool_projects = (
        db.query(Project)
        .filter(
            Project.host_id.in_(pool_host_ids),
            Project.state.in_(["active", "stopped"]),
        )
        .all()
    )

    referenced_items = _collect_referenced_items(pool_projects)

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
        .filter(SharedCacheEntry.storage_pool_id == pool_id)
        .count()
    )
    report["cache_entries_stale"] = len(stale_entries)
    report["cache_entries_evictable"] = len(evictable)

    _evict_stale_cache_entries(db, pool_id, scan_host, evictable, dry_run, report)


def _pool_orphan_cleanup(db, scan_host, dry_run, report):
    """Discover and clean orphans on shared storage via a scan host."""
    if scan_host.agent_status != "connected":
        return

    orphans = discover_orphans(db, scan_host)
    report["orphans"] = orphans

    total_orphans = _count_total_orphans(orphans)
    cache_count = len(orphans.get("cache_items", []))
    stale_count = len(orphans.get("stale_temps", []))
    report["orphans_found"] = total_orphans

    cleanable = total_orphans + cache_count + stale_count
    if cleanable > 0 and not dry_run:
        cleanup = clean_orphans(scan_host, orphans, db)
        report["cleanup"] = cleanup
    elif cleanable > 0:
        report["cleanup"] = {"dry_run": True, "would_clean": cleanable}


def reconcile_pool(pool_id: str, dry_run: bool = False) -> dict:
    """Pool-level GC for shared storage. Uses any connected host in the pool to scan the filesystem."""
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.storage_pool import StoragePool

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

        # 1. Capacity sync
        from app.services.troshkad_client import check_disk_usage

        usage = check_disk_usage(scan_host)
        report["shared_storage"] = usage
        log.info("Pool GC %s: shared storage usage: %s", pool_id[:8], usage)

        # 2. Sync capacity for all hosts in pool
        hosts_in_pool = (
            db.query(Host)
            .filter(Host.storage_pool_id == pool_id, Host.state == "active")
            .all()
        )
        for h in hosts_in_pool:
            sync_host_capacity(db, h)
        report["hosts_synced"] = len(hosts_in_pool)

        # 3. Cache eviction
        _pool_cache_eviction(db, pool_id, hosts_in_pool, scan_host, dry_run, report)

        # 4. Network repair
        if not dry_run:
            for h in hosts_in_pool:
                if h.agent_status == "connected":
                    repair_networks(db, h)

        # 5. Orphan cleanup
        _pool_orphan_cleanup(db, scan_host, dry_run, report)

        log.info("Pool GC %s: complete — %s", pool_id[:8], report)
        return report

    except Exception as e:
        log.exception("Pool GC failed for %s: %s", pool_id[:8], e)
        return {"error": str(e)}
    finally:
        db.close()
