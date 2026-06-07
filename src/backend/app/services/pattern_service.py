"""
Pattern service — captures VM disk snapshots to S3 for pattern storage.
"""
import logging

from app.core.database import SessionLocal
from app.models.pattern import Pattern, PatternDisk

log = logging.getLogger(__name__)

_capture_progress: dict[str, dict] = {}


def get_capture_progress(pattern_id: str) -> dict | None:
    """Return capture progress for a pattern, or None if not tracking."""
    return _capture_progress.get(pattern_id)


def capture_pattern_disks(pattern_id: str, project_id: str) -> None:
    """Capture all disks from a project into a pattern.

    Runs in a background thread, spawned by the patterns API when creating from a source project.
    Uploads each disk to S3 via SSH on the host, creates PatternDisk records, and updates pattern state.
    """
    from app.models.project import Project
    from app.models.host import Host
    from app.services import s3_storage
    from app.services.deploy_service import run_ssh_script

    db = SessionLocal()
    try:
        pattern = db.query(Pattern).filter_by(id=pattern_id).first()
        project = db.query(Project).filter_by(id=project_id).first()
        if not pattern or not project:
            log.error("Pattern or project not found: %s / %s", pattern_id, project_id)
            return

        host = db.query(Host).filter_by(id=project.host_id).first()
        if not host:
            pattern.state = "error"
            db.commit()
            log.error("No host found for project %s", project_id)
            return

        topology = project.deployed_topology or project.topology or {"nodes": [], "edges": []}
        disk_nodes = [n for n in topology.get("nodes", []) if n.get("type") == "storageNode"]
        vm_nodes = {n["id"]: n for n in topology.get("nodes", []) if n.get("type") == "vmNode"}

        edges = topology.get("edges", [])
        disk_to_vm = {}
        for edge in edges:
            src, tgt = edge.get("source"), edge.get("target")
            if src in vm_nodes and tgt in [d["id"] for d in disk_nodes]:
                disk_to_vm[tgt] = src
            elif tgt in vm_nodes and src in [d["id"] for d in disk_nodes]:
                disk_to_vm[src] = tgt

        total = len(disk_nodes)
        for idx, disk_node in enumerate(disk_nodes):
            disk_id = disk_node["id"]
            vm_id = disk_to_vm.get(disk_id, "unknown")
            fmt = disk_node.get("data", {}).get("format", "qcow2")

            if fmt == "iso":
                continue

            s3_key = f"patterns/{pattern_id}/{disk_id}.{fmt}"

            _capture_progress[pattern_id] = {
                "step": "uploading",
                "detail": f"disk {idx + 1}/{total}",
                "disk_id": disk_id,
            }

            disk_path = f"/var/lib/troshka/vms/{project_id}/{vm_id[:8]}-{disk_id[:8]}.{fmt}"
            presigned = s3_storage.generate_presigned_upload_url(s3_key, expires=7200)

            script = f'''set -e
DISK_PATH="{disk_path}"
UPLOAD_URL='{presigned}'

if [ ! -f "$DISK_PATH" ]; then
    echo "ERROR: disk not found at $DISK_PATH"
    exit 1
fi

curl -s -X PUT -T "$DISK_PATH" "$UPLOAD_URL"
echo "UPLOAD_COMPLETE"
'''
            result = run_ssh_script(host.ip_address, host.private_key, script, timeout=3600)

            pd = PatternDisk(
                pattern_id=pattern_id,
                source_disk_id=disk_id,
                source_vm_id=vm_id,
                s3_key=s3_key,
                format=fmt,
                size_bytes=0,
                virtual_size_bytes=int(disk_node.get("data", {}).get("size", 0)) * 1073741824,
                state="available" if result["success"] else "error",
            )
            db.add(pd)
            db.commit()

            if not result["success"]:
                log.error("Failed to upload disk %s: %s", disk_id, result.get("output", ""))
                pattern.state = "error"
                db.commit()
                return

        pattern.state = "available"
        pattern.total_size_bytes = sum(d.size_bytes for d in pattern.disks)
        db.commit()
        log.info("Pattern %s capture complete", pattern_id)

    except Exception as e:
        log.exception("Pattern capture failed for %s: %s", pattern_id, e)
        try:
            pattern = db.query(Pattern).filter_by(id=pattern_id).first()
            if pattern:
                pattern.state = "error"
                db.commit()
        except Exception:
            pass
    finally:
        _capture_progress.pop(pattern_id, None)
        db.close()
