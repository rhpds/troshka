"""VM snapshot disk capture — upload a single VM's disks to S3."""

import logging

log = logging.getLogger(__name__)


def capture_vm_disks(library_item_id: str, project_id: str, vm_node_id: str) -> None:
    from app.core.database import SessionLocal
    from app.models.host import Host
    from app.models.library import LibraryItem, LibraryItemDisk
    from app.models.project import Project
    from app.services import s3_storage
    from app.services.troshkad_client import TroshkadError, start_job, wait_for_job

    db = SessionLocal()
    try:
        item = db.query(LibraryItem).filter_by(id=library_item_id).first()
        project = db.query(Project).filter_by(id=project_id).first()
        if not item or not project:
            log.error(
                "Snapshot item or project not found: %s / %s",
                library_item_id,
                project_id,
            )
            return

        host = db.query(Host).filter_by(id=project.host_id).first()
        if not host or not host.ip_address:
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
            log.info(
                "Snapshot %s: VM has no disks, marking available", library_item_id[:8]
            )
            return

        # Build domain name for the VM
        domain_name = f"troshka-{project_id[:8]}-{vm_node_id[:8]}"

        for idx, disk_node in enumerate(disk_nodes):
            disk_id = disk_node["id"]
            fmt = disk_node.get("data", {}).get("format", "qcow2")

            if fmt == "iso":
                continue

            s3_key = f"snapshots/{library_item_id}/{disk_id}.{fmt}"
            bucket = s3_storage._bucket()
            s3_url = f"s3://{bucket}/{s3_key}"
            cache_path = (
                f"/var/lib/troshka/cache/snapshots/{library_item_id}/{disk_id}.{fmt}"
            )

            from app.services.s3_storage import _get_s3_config

            creds = _get_s3_config()

            try:
                job_id = start_job(
                    host,
                    "/snapshots/capture",
                    {
                        "domain_name": domain_name,
                        "disk_index": idx,
                        "s3_url": s3_url,
                        "cache_path": cache_path,
                        "aws_access_key_id": creds.get("access_key_id", ""),
                        "aws_secret_access_key": creds.get("secret_access_key", ""),
                        "aws_region": creds.get("region", "us-east-1"),
                    },
                )
                job = wait_for_job(host, job_id, timeout=3600)

                if job["status"] == "failed":
                    error_msg = job.get("result", {}).get(
                        "error", "Snapshot capture failed"
                    )
                    log.error(
                        "Snapshot %s: failed to upload disk %s: %s",
                        library_item_id[:8],
                        disk_id[:8],
                        error_msg,
                    )
                    item.state = "error"
                    db.commit()
                    return

                size_bytes = job.get("result", {}).get("size_bytes", 0)

                disk_record = LibraryItemDisk(
                    library_item_id=library_item_id,
                    s3_key=s3_key,
                    format=fmt,
                    size_bytes=size_bytes,
                    virtual_size_bytes=int(disk_node.get("data", {}).get("size", 0))
                    * 1073741824,
                    boot_order=idx,
                    state="available",
                )
                db.add(disk_record)
                db.commit()

            except TroshkadError as e:
                log.error(
                    "Snapshot %s: troshkad error uploading disk %s: %s",
                    library_item_id[:8],
                    disk_id[:8],
                    str(e),
                )
                item.state = "error"
                db.commit()
                return

        item.size_bytes = sum(d.size_bytes for d in item.item_disks)
        if item.item_disks:
            item.s3_key = item.item_disks[0].s3_key
        item.state = "ready"
        db.commit()

        # Save metadata to S3 for recovery after DB loss
        import json

        metadata = {
            "type": "snapshot",
            "name": item.name,
            "item_type": item.type,
            "format": item.format,
            "size_bytes": item.size_bytes,
            "os_variant": item.os_variant,
            "vm_config": item.vm_config,
            "tags": item.tags,
            "disks": [
                {
                    "s3_key": d.s3_key,
                    "format": d.format,
                    "size_bytes": d.size_bytes,
                    "virtual_size_bytes": d.virtual_size_bytes,
                    "boot_order": d.boot_order,
                }
                for d in item.item_disks
            ],
        }
        try:
            s3_storage._get_s3_client().put_object(
                Bucket=s3_storage._bucket(),
                Key=f"snapshots/{library_item_id}/metadata.json",
                Body=json.dumps(metadata),
                ContentType="application/json",
            )
        except Exception:
            log.warning(
                "Failed to save snapshot metadata to S3 for %s", library_item_id[:8]
            )

        log.info(
            "Snapshot %s: capture complete (%d disks)",
            library_item_id[:8],
            len(disk_nodes),
        )

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
