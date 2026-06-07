"""VM snapshot disk capture — upload a single VM's disks to S3."""
import logging

log = logging.getLogger(__name__)


def capture_vm_disks(library_item_id: str, project_id: str, vm_node_id: str) -> None:
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.library import LibraryItem, LibraryItemDisk
    from app.models.project import Project
    from app.services import s3_storage
    from app.services.deploy_service import run_ssh_script

    db = SessionLocal()
    try:
        item = db.query(LibraryItem).filter_by(id=library_item_id).first()
        project = db.query(Project).filter_by(id=project_id).first()
        if not item or not project:
            log.error("Snapshot item or project not found: %s / %s", library_item_id, project_id)
            return

        host = db.query(Host).filter_by(id=project.host_id).first()
        if not host or not host.ip_address or not host.private_key:
            item.state = "error"
            db.commit()
            log.error("No reachable host for project %s", project_id)
            return

        topology = project.deployed_topology or project.topology or {}
        edges = topology.get("edges", [])
        disk_nodes = []
        for node in topology.get("nodes", []):
            if node.get("type") != "storageNode":
                continue
            connected = any(
                (e.get("source") == vm_node_id and e.get("target") == node["id"])
                or (e.get("target") == vm_node_id and e.get("source") == node["id"])
                for e in edges
            )
            if connected:
                disk_nodes.append(node)

        if not disk_nodes:
            item.state = "available"
            db.commit()
            log.info("Snapshot %s: VM has no disks, marking available", library_item_id[:8])
            return

        for idx, disk_node in enumerate(disk_nodes):
            disk_id = disk_node["id"]
            fmt = disk_node.get("data", {}).get("format", "qcow2")

            if fmt == "iso":
                continue

            s3_key = f"snapshots/{library_item_id}/{disk_id}.{fmt}"
            disk_path = f"/var/lib/troshka/vms/{project_id}/{vm_node_id[:8]}-{disk_id[:8]}.{fmt}"
            presigned = s3_storage.generate_presigned_upload_url(s3_key, expires=7200)

            script = f'''set -e
DISK_PATH="{disk_path}"
UPLOAD_URL='{presigned}'

if [ ! -f "$DISK_PATH" ]; then
    echo "ERROR: disk not found at $DISK_PATH"
    exit 1
fi

SIZE=$(stat -c %s "$DISK_PATH" 2>/dev/null || echo 0)
echo "Uploading $DISK_PATH ($SIZE bytes)"
curl -s -X PUT -T "$DISK_PATH" "$UPLOAD_URL"
echo "SIZE:$SIZE"
echo "UPLOAD_COMPLETE"
'''
            result = run_ssh_script(host.ip_address, host.private_key, script, timeout=3600)

            size_bytes = 0
            for line in result.get("output", "").splitlines():
                if line.startswith("SIZE:"):
                    size_bytes = int(line.split(":")[1])

            disk_record = LibraryItemDisk(
                library_item_id=library_item_id,
                s3_key=s3_key,
                format=fmt,
                size_bytes=size_bytes,
                virtual_size_bytes=int(disk_node.get("data", {}).get("size", 0)) * 1073741824,
                boot_order=idx,
                state="available" if result["success"] else "error",
            )
            db.add(disk_record)
            db.commit()

            if not result["success"]:
                log.error("Snapshot %s: failed to upload disk %s: %s", library_item_id[:8], disk_id[:8], result.get("output", "")[:200])
                item.state = "error"
                db.commit()
                return

        item.size_bytes = sum(d.size_bytes for d in item.item_disks)
        item.state = "ready"
        db.commit()
        log.info("Snapshot %s: capture complete (%d disks)", library_item_id[:8], len(disk_nodes))

    except Exception as e:
        log.exception("Snapshot capture failed for %s: %s", library_item_id[:8], e)
        try:
            item = db.query(LibraryItem).filter_by(id=library_item_id).first()
            if item:
                item.state = "error"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()
