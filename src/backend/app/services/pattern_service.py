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
    Uploads each disk to S3 via troshkad on the host, creates PatternDisk records, and updates pattern state.
    """
    from app.models.project import Project
    from app.models.host import Host
    from app.services import s3_storage
    from app.services.troshkad_client import start_job, wait_for_job, TroshkadError

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

        # Group disks by VM so we can make one troshkad call per VM
        vm_to_disks = {}
        for disk_node in disk_nodes:
            vm_id = disk_to_vm.get(disk_node["id"])
            if not vm_id:
                continue
            if vm_id not in vm_to_disks:
                vm_to_disks[vm_id] = []
            vm_to_disks[vm_id].append(disk_node)

        total = len(disk_nodes)
        processed = 0

        for vm_id, vm_disk_nodes in vm_to_disks.items():
            domain_name = f"troshka-{project_id[:8]}-{vm_id[:8]}"

            # Build disk parameters for this VM's disks
            disks_params = []
            disk_metadata = []  # Keep track for DB records
            for disk_node in vm_disk_nodes:
                disk_id = disk_node["id"]
                fmt = disk_node.get("data", {}).get("format", "qcow2")

                if fmt == "iso":
                    continue

                # Find disk index by looking at the VM's attached disks
                disk_index = 0
                for i, other_disk in enumerate(vm_disk_nodes):
                    if other_disk["id"] == disk_id:
                        disk_index = i
                        break

                s3_key = f"patterns/{pattern_id}/{disk_id}.{fmt}"
                presigned = s3_storage.generate_presigned_upload_url(s3_key, expires=7200)
                cache_path = f"/var/lib/troshka/cache/patterns/{pattern_id}/{disk_id}.{fmt}"

                disks_params.append({
                    "disk_index": disk_index,
                    "presigned_url": presigned,
                    "cache_path": cache_path,
                })

                disk_metadata.append({
                    "disk_id": disk_id,
                    "vm_id": vm_id,
                    "s3_key": s3_key,
                    "format": fmt,
                    "virtual_size_bytes": int(disk_node.get("data", {}).get("size", 0)) * 1073741824,
                })

            if not disks_params:
                continue

            _capture_progress[pattern_id] = {
                "step": "uploading",
                "detail": f"VM {vm_id[:8]} ({len(disks_params)} disks)",
                "vm_id": vm_id,
            }

            try:
                job_id = start_job(host, "/patterns/capture", {
                    "domain_name": domain_name,
                    "disks": disks_params,
                })
                job = wait_for_job(host, job_id, timeout=3600)

                if job["status"] == "failed":
                    error_msg = job.get("result", {}).get("error", "Pattern capture failed")
                    log.error("Failed to capture pattern %s VM %s: %s", pattern_id[:8], vm_id[:8], error_msg)
                    pattern.state = "error"
                    db.commit()
                    return

                # Extract size results for each disk
                disk_results = job.get("result", {}).get("disks", [])

                # Create PatternDisk records
                for i, metadata in enumerate(disk_metadata):
                    size_bytes = 0
                    if i < len(disk_results):
                        size_bytes = disk_results[i].get("size_bytes", 0)

                    pd = PatternDisk(
                        pattern_id=pattern_id,
                        source_disk_id=metadata["disk_id"],
                        source_vm_id=metadata["vm_id"],
                        s3_key=metadata["s3_key"],
                        format=metadata["format"],
                        size_bytes=size_bytes,
                        virtual_size_bytes=metadata["virtual_size_bytes"],
                        state="available",
                    )
                    db.add(pd)

                db.commit()
                processed += len(disk_metadata)

            except TroshkadError as e:
                log.error("Troshkad error capturing pattern %s VM %s: %s", pattern_id[:8], vm_id[:8], str(e))
                pattern.state = "error"
                db.commit()
                return

        # Update pattern topology: point storage nodes to captured pattern disks
        topo = pattern.topology or {}
        disk_map = {d.source_disk_id: d for d in pattern.disks}
        for node in topo.get("nodes", []):
            if node.get("type") != "storageNode":
                continue
            if node.get("data", {}).get("format") == "iso":
                continue
            pd = disk_map.get(node["id"])
            if pd:
                node["data"]["source"] = "pattern"
                node["data"]["patternId"] = pattern_id
                node["data"]["patternDiskId"] = pd.id
                node["data"].pop("libraryItemId", None)
        import json, copy
        from sqlalchemy import text
        db.execute(
            text("UPDATE patterns SET topology = :topo WHERE id = :pid"),
            {"topo": json.dumps(copy.deepcopy(topo)), "pid": pattern_id},
        )

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
