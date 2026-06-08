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

    # Call troshkad to discover orphans
    job_id = start_job(host, "/gc/discover", {
        "known_project_ids": active_project_ids,
        "known_domains": known_domains,
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
        "orphan_dirs": [op.get("project_id") if isinstance(op, dict) else op for op in orphans.get("orphaned_projects", [])],
        "orphan_domains": orphans.get("orphaned_domains", []),
        "orphan_bridges": orphans.get("orphaned_bridges", []),
        "orphan_namespaces": orphans.get("orphaned_namespaces", []),
        "cache_items": cache_items,
    })
    job = wait_for_job(host, job_id, timeout=120)

    cleaned = len(orphans.get("orphaned_projects", [])) + len(orphans.get("orphaned_domains", [])) + len(orphans.get("orphaned_bridges", []))
    return {
        "success": job["status"] == "completed",
        "cleaned": cleaned,
        "output": "\n".join(job.get("output", [])),
    }


def repair_networks(db: Session, host) -> dict:
    """Ensure VXLAN bridges exist for all active/stopped projects on this host."""
    from app.models.project import Project
    from app.services.deploy_service import run_ssh_script
    from app.services.vxlan import build_host_network_config, generate_setup_script

    if not host.ip_address or not host.private_key:
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

    check_script = "ip -o link show type bridge 2>/dev/null | grep -oP 'br-\\d+' || true"
    result = run_ssh_script(host.ip_address, host.private_key, check_script, timeout=15)
    existing_bridges = set(result.get("output", "").strip().splitlines()) if result["success"] else set()

    missing_vnis = {vni for vni in needed_vnis if f"br-{vni}" not in existing_bridges}
    if not missing_vnis:
        return {"repaired": 0, "all_bridges_present": True}

    repaired = 0
    for p in projects:
        project_vnis = set(str(v) for v in (p.vni_map or {}).values())
        if not project_vnis.intersection(missing_vnis):
            continue
        topo = p.deployed_topology or p.topology or {}
        config = build_host_network_config(topo, p.vni_map or {}, [])
        script = generate_setup_script(config, host.ip_address, p.id)
        r = run_ssh_script(host.ip_address, host.private_key, script, timeout=30)
        if r["success"]:
            repaired += len(project_vnis.intersection(missing_vnis))
            log.info("Repaired bridges for project %s", p.id[:8])

    return {"repaired": repaired, "missing_bridges": len(missing_vnis)}


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

        report["capacity"] = sync_host_capacity(db, host)

        if not host.ip_address or host.agent_status != "connected":
            report["orphans"] = {"error": "Host not reachable — skipping orphan scan"}
            return report

        orphans = discover_orphans(db, host)
        report["orphans"] = orphans

        if orphans.get("error"):
            return report

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
            cleanup = clean_orphans(host, orphans)
            report["cleanup"] = cleanup
            log.info("Host %s GC: cleaned %d orphans", host_id[:8], cleanup["cleaned"])
        elif total_orphans > 0:
            report["cleanup"] = {"dry_run": True, "would_clean": total_orphans}
        else:
            report["cleanup"] = {"cleaned": 0}
            log.info("Host %s GC: no orphans found", host_id[:8])

        if not dry_run:
            network_repair = repair_networks(db, host)
            report["network_repair"] = network_repair
            if network_repair.get("repaired", 0) > 0:
                log.info("Host %s GC: repaired %d bridges", host_id[:8], network_repair["repaired"])

        return report

    except Exception as e:
        log.exception("GC failed for host %s: %s", host_id[:8], e)
        return {"error": str(e)}
    finally:
        db.close()
