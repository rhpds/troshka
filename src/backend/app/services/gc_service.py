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

    return {
        "orphaned_projects": orphaned_projects,
        "orphaned_domains": orphaned_domains,
        "orphaned_bridges": orphaned_bridges,
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
        lines.append(f"ip link del {bridge} 2>/dev/null || true")
        lines.append(f"ip link del vxlan-{vni} 2>/dev/null || true")
        lines.append(f"rm -f /etc/dnsmasq.d/troshka-{vni}.conf 2>/dev/null || true")

    if len(lines) <= 3:
        return {"cleaned": 0, "output": "Nothing to clean"}

    lines.append("")
    lines.append("systemctl restart dnsmasq 2>/dev/null || true")
    lines.append('echo "GC cleanup complete"')

    result = run_ssh_script(host.ip_address, host.private_key, "\n".join(lines), timeout=120)

    cleaned = len(orphans.get("orphaned_projects", [])) + len(orphans.get("orphaned_domains", [])) + len(orphans.get("orphaned_bridges", []))
    return {
        "success": result["success"],
        "cleaned": cleaned,
        "output": result.get("output", ""),
    }


def reconcile_host(host_id: str, dry_run: bool = False) -> dict:
    """Full reconciliation: sync capacity + discover + clean orphans."""
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

        return report

    except Exception as e:
        log.exception("GC failed for host %s: %s", host_id[:8], e)
        return {"error": str(e)}
    finally:
        db.close()
