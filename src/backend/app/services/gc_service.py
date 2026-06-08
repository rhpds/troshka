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
    """SSH to host, list what's there, cross-reference with DB."""
    from app.models.project import Project
    from app.services.deploy_service import run_ssh_script

    if not host.ip_address or not host.private_key:
        return {"error": "Host not reachable", "orphaned_projects": [], "orphaned_domains": [], "orphaned_bridges": []}

    script = """#!/bin/bash
echo "=== DIRS ==="
ls -1 /var/lib/troshka/vms/ 2>/dev/null || true
echo "=== DOMAINS ==="
virsh list --all --name 2>/dev/null | grep -v '^$' || true
echo "=== BRIDGES ==="
ip -o link show type bridge 2>/dev/null | grep -oP 'br-\\d+' || true
echo "=== DIR_TIMES ==="
for d in /var/lib/troshka/vms/*/; do
    [ -d "$d" ] && echo "$(basename $d) $(stat -c %Y $d 2>/dev/null || echo 0)"
done
echo "=== CACHED_PATTERNS ==="
ls -1 /var/lib/troshka/cache/patterns/ 2>/dev/null || true
echo "=== CACHED_SNAPSHOTS ==="
ls -1 /var/lib/troshka/cache/snapshots/ 2>/dev/null || true
echo "=== CACHED_IMAGES ==="
ls -1 /var/lib/troshka/images/ 2>/dev/null | sed 's/\\.[^.]*$//' | sort -u || true
echo "=== CACHE_ATIME ==="
for d in /var/lib/troshka/cache/patterns/*/  /var/lib/troshka/cache/snapshots/*/; do
    [ -d "$d" ] || continue
    LATEST=0
    for f in "$d"*; do
        [ -f "$f" ] || continue
        AT=$(stat -c %X "$f" 2>/dev/null || echo 0)
        [ "$AT" -gt "$LATEST" ] && LATEST=$AT
    done
    TYPE=$(echo "$d" | grep -oP '(patterns|snapshots)')
    echo "$TYPE/$(basename $d) $LATEST"
done
for f in /var/lib/troshka/images/*; do
    [ -f "$f" ] || continue
    BASENAME=$(basename "$f" | sed 's/\\.[^.]*$//')
    AT=$(stat -c %X "$f" 2>/dev/null || echo 0)
    echo "images/$BASENAME $AT"
done
echo "=== END ==="
"""
    result = run_ssh_script(host.ip_address, host.private_key, script, timeout=30)
    if not result["success"]:
        return {"error": f"SSH failed: {result.get('output', '')[:200]}", "orphaned_projects": [], "orphaned_domains": [], "orphaned_bridges": []}

    output = result["output"]
    sections = {}
    current = None
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("=== ") and line.endswith(" ==="):
            current = line[4:-4]
            sections[current] = []
        elif current and current != "END" and line:
            sections[current].append(line)

    host_dirs = sections.get("DIRS", [])
    host_domains = sections.get("DOMAINS", [])
    host_bridges = sections.get("BRIDGES", [])
    dir_times = {}
    for entry in sections.get("DIR_TIMES", []):
        parts = entry.split()
        if len(parts) == 2:
            dir_times[parts[0]] = int(parts[1])

    now = int(time.time())

    active_project_ids = set()
    active_vni_values = set()
    skip_states = {"deploying", "reconfiguring"}

    for p in db.query(Project).filter(Project.host_id == host.id).all():
        if p.state in skip_states:
            active_project_ids.add(p.id)
            continue
        if p.state in ("active", "stopped"):
            active_project_ids.add(p.id)
            for vni in (p.vni_map or {}).values():
                active_vni_values.add(str(vni))

    orphaned_projects = []
    for d in host_dirs:
        if d in active_project_ids:
            continue
        dir_age = now - dir_times.get(d, 0)
        if dir_age < 300:
            continue
        exists_in_db = db.query(Project).filter_by(id=d).first()
        orphaned_projects.append({
            "project_id": d,
            "reason": "not in DB" if not exists_in_db else f"state={exists_in_db.state}, assigned to different host" if exists_in_db.host_id != host.id else f"state={exists_in_db.state}",
        })

    orphaned_domains = []
    for domain in host_domains:
        if not domain.startswith("troshka-"):
            continue
        project_prefix = domain.split("-")[1] if "-" in domain else ""
        matched = any(pid.startswith(project_prefix) for pid in active_project_ids)
        if not matched:
            orphaned_domains.append(domain)

    orphaned_bridges = []
    for bridge in host_bridges:
        vni = bridge.replace("br-", "")
        if vni not in active_vni_values:
            orphaned_bridges.append(bridge)

    from app.models.pattern import Pattern
    from app.models.library import LibraryItem

    cached_patterns = sections.get("CACHED_PATTERNS", [])
    cached_snapshots = sections.get("CACHED_SNAPSHOTS", [])

    cache_atimes = {}
    for entry in sections.get("CACHE_ATIME", []):
        parts = entry.split()
        if len(parts) == 2:
            cache_atimes[parts[0]] = int(parts[1])

    from app.core.config import config
    gc_cfg = getattr(config, "gc", None)
    stale_hours_patterns = getattr(gc_cfg, "cache_stale_hours_patterns", 24) if gc_cfg else 24
    stale_hours_snapshots = getattr(gc_cfg, "cache_stale_hours_snapshots", 1) if gc_cfg else 1
    stale_hours_images = getattr(gc_cfg, "cache_stale_hours_images", 1) if gc_cfg else 1
    stale_thresholds = {
        "patterns": now - (stale_hours_patterns * 3600),
        "snapshots": now - (stale_hours_snapshots * 3600),
        "images": now - (stale_hours_images * 3600),
    }

    orphaned_cache = []
    stale_cache = []
    for pid in cached_patterns:
        if not db.query(Pattern).filter_by(id=pid).first():
            orphaned_cache.append(f"patterns/{pid}")
        else:
            atime = cache_atimes.get(f"patterns/{pid}", 0)
            if atime > 0 and atime < stale_thresholds["patterns"]:
                hours_ago = (now - atime) // 3600
                stale_cache.append({"path": f"patterns/{pid}", "last_accessed_hours_ago": hours_ago})
    for sid in cached_snapshots:
        if not db.query(LibraryItem).filter_by(id=sid, type="snapshot").first():
            orphaned_cache.append(f"snapshots/{sid}")
        else:
            atime = cache_atimes.get(f"snapshots/{sid}", 0)
            if atime > 0 and atime < stale_thresholds["snapshots"]:
                hours_ago = (now - atime) // 3600
                stale_cache.append({"path": f"snapshots/{sid}", "last_accessed_hours_ago": hours_ago})

    cached_images = sections.get("CACHED_IMAGES", [])
    for iid in cached_images:
        if not db.query(LibraryItem).filter_by(id=iid).first():
            orphaned_cache.append(f"images/{iid}")
        else:
            atime = cache_atimes.get(f"images/{iid}", 0)
            if atime > 0 and atime < stale_thresholds["images"]:
                hours_ago = (now - atime) // 3600
                stale_cache.append({"path": f"images/{iid}", "last_accessed_hours_ago": hours_ago})

    return {
        "orphaned_projects": orphaned_projects,
        "orphaned_domains": orphaned_domains,
        "orphaned_bridges": orphaned_bridges,
        "orphaned_cache": orphaned_cache,
        "stale_cache": stale_cache,
        "host_dirs": len(host_dirs),
        "host_domains": len(host_domains),
        "host_bridges": len(host_bridges),
    }


def clean_orphans(host, orphans: dict) -> dict:
    """SSH to host and remove orphaned resources."""
    from app.services.deploy_service import run_ssh_script

    if not host.ip_address or not host.private_key:
        return {"error": "Host not reachable", "cleaned": 0}

    lines = ["#!/bin/bash", "set -uo pipefail", ""]

    for op in orphans.get("orphaned_projects", []):
        pid = op["project_id"]
        lines.append(f'echo "Cleaning orphaned project dir: {pid[:8]}..."')
        lines.append(f"rm -rf /var/lib/troshka/vms/{pid}")

    for domain in orphans.get("orphaned_domains", []):
        lines.append(f'echo "Removing orphaned domain: {domain}"')
        lines.append(f"virsh destroy {domain} 2>/dev/null || true")
        lines.append(f"virsh undefine {domain} 2>/dev/null || true")

    for bridge in orphans.get("orphaned_bridges", []):
        vni = bridge.replace("br-", "")
        lines.append(f'echo "Removing orphaned bridge: {bridge}"')
        lines.append(f"[ -f /run/troshka-dnsmasq-{vni}.pid ] && kill $(cat /run/troshka-dnsmasq-{vni}.pid) 2>/dev/null || true")
        lines.append(f"rm -f /run/troshka-dnsmasq-{vni}.pid /etc/dnsmasq.d/troshka-{vni}.conf /var/lib/troshka/dnsmasq-{vni}.leases")
        lines.append(f"ip link del {bridge} 2>/dev/null || true")
        lines.append(f"ip link del vxlan-{vni} 2>/dev/null || true")

    for cache_path in orphans.get("orphaned_cache", []):
        lines.append(f'echo "Removing orphaned cache: {cache_path}"')
        if cache_path.startswith("images/"):
            item_id = cache_path.split("/")[1]
            lines.append(f"rm -f /var/lib/troshka/images/{item_id}.* 2>/dev/null || true")
        else:
            lines.append(f"rm -rf /var/lib/troshka/cache/{cache_path}")

    for entry in orphans.get("stale_cache", []):
        path = entry["path"]
        lines.append(f'echo "Evicting stale cache ({entry["last_accessed_hours_ago"]}h old): {path}"')
        if path.startswith("images/"):
            item_id = path.split("/")[1]
            lines.append(f"rm -f /var/lib/troshka/images/{item_id}.* 2>/dev/null || true")
        else:
            lines.append(f"rm -rf /var/lib/troshka/cache/{path}")

    if len(lines) <= 3:
        return {"cleaned": 0, "output": "Nothing to clean"}

    lines.append("")
    lines.append('echo "GC cleanup complete"')

    result = run_ssh_script(host.ip_address, host.private_key, "\n".join(lines), timeout=120)

    cleaned = len(orphans.get("orphaned_projects", [])) + len(orphans.get("orphaned_domains", [])) + len(orphans.get("orphaned_bridges", []))
    return {
        "success": result["success"],
        "cleaned": cleaned,
        "output": result.get("output", ""),
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

        if not host.ip_address or not host.private_key:
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
